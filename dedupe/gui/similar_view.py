"""Similar Images tab: groups of images that look the same but are different files.

A flat table model that emulates a tree, like the Duplicates tab (see duplicates_view.py for why),
loaded in small batches from the event loop. Two things differ from exact duplicates:

* **Nothing is pre-selected.** The recommender still marks a keeper (the highest resolution), and
  the "Suggestion" column and the compare panel show it, but only the user's own choices put an
  image in the delete selection ("Select suggested" is a button, not a default).
* The images differ, so the delete flow warns that the content of a deleted image will not exist
  anywhere else, and never offers hard links.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QContextMenuEvent,
    QDesktopServices,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.actions import similar_groups_after
from dedupe.core.formatting import human
from dedupe.core.models import (
    CancelToken,
    ProgressCallback,
    SimilarGroup,
    SimilarMember,
    Verdict,
)
from dedupe.core.recommender import is_protected, recommend_similar_all
from dedupe.gui import icons
from dedupe.gui.details_panel import DetailsPanel
from dedupe.gui.duplicates_view import (
    BATCH_BUDGET,
    INDENT,
    NODE_ROLE,
    ROW_HEIGHT,
    Index,
    TreeDelegate,
)
from dedupe.gui.gcutil import freeze
from dedupe.gui.image_compare import ImageComparePanel
from dedupe.gui.thumbnails import ThumbnailService
from dedupe.gui.workers import Job, JobRunner

COLUMNS = (
    "Group / Image",
    "Dimensions",
    "Size",
    "Similar",
    "Exact copies",
    "Modified",
    "Suggestion",
)
COL_ITEM, COL_DIMS, COL_SIZE, COL_SIMILAR, COL_EXACT, COL_MODIFIED, COL_SUGGEST = range(7)
RIGHT = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

WARNING_TEXT = (
    "These images are similar, not identical. Nothing is selected for you, and the content of an "
    "image you delete will not exist anywhere else."
)
OFF_TEXT = "Similar-image search is off. Turn on “Find similar images” in Settings, then scan."


class MemberNode:
    is_group = False
    __slots__ = ("group", "index", "member", "override", "protected", "reason", "suggested")

    def __init__(self, member: SimilarMember, group: SimilarGroupNode, index: int) -> None:
        self.member = member
        self.group = group
        self.index = index
        self.suggested = False  # the recommender would delete it: shown, never acted on
        self.override: bool | None = None  # the user's explicit choice
        self.protected = False
        self.reason = ""

    @property
    def entry(self) -> Any:
        return self.member.entry

    @property
    def checked(self) -> bool:
        """Selected for deletion: only ever by the user's explicit choice."""
        return not self.protected and bool(self.override)


class SimilarGroupNode:
    is_group = True
    __slots__ = ("expanded", "group", "index", "members", "row")

    def __init__(self, group: SimilarGroup) -> None:
        self.group = group
        self.index = -1
        self.row = -1
        self.expanded = False
        self.members: list[MemberNode] = []


Node = MemberNode | SimilarGroupNode


class SimilarModel(QAbstractTableModel):
    selection_changed = Signal()
    load_finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._groups: list[SimilarGroupNode] = []
        self._rows: list[Node] = []
        self._by_path: dict[Path, MemberNode] = {}
        self._pending: list[SimilarGroup] = []
        self._pending_pos = 0
        self._protected: tuple[str, ...] = ()
        self._overrides: dict[Path, bool] = {}
        self._live_overrides: dict[Path, bool] = {}
        self._selected: set[Path] = set()
        self._apply_iter: Iterator[tuple[Path, bool | None]] = iter(())
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(0)
        self._apply_timer.timeout.connect(self._apply_slice)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._load_batch)
        self.selected_size = 0
        self.selected_count = 0
        self.loading = False
        self._badges = self._load_badges()

    @staticmethod
    def _load_badges() -> dict[str, Any]:
        return {
            "keep": icons.icon("badge-keep"),
            "delete": icons.icon("badge-delete"),
            "protected": icons.icon("badge-protected"),
        }

    # -- loading -------------------------------------------------------------------------

    def set_groups(
        self,
        groups: Sequence[SimilarGroup],
        protected_folders: Iterable[str] = (),
        overrides: dict[Path, bool] | None = None,
    ) -> None:
        """Replace the contents; rows arrive in batches (see ``load_finished``)."""
        self._timer.stop()
        self._apply_timer.stop()
        self._apply_iter = iter(())
        self._overrides = overrides or {}
        self._live_overrides = dict(self._overrides)
        self.beginResetModel()
        self._groups = []
        self._rows = []
        self._by_path = {}
        self.selected_size = 0
        self.selected_count = 0
        self._selected = set()
        self._protected = tuple(protected_folders)
        self._pending = list(groups)
        self._pending_pos = 0
        self.endResetModel()
        self.loading = bool(self._pending)
        self.selection_changed.emit()
        if self.loading:
            self._timer.start()
        else:
            self.load_finished.emit()

    def _load_batch(self) -> None:
        start = time.perf_counter()
        batch: list[SimilarGroupNode] = []
        while self._pending_pos < len(self._pending):
            batch.append(self._build(self._pending[self._pending_pos]))
            self._pending_pos += 1
            if time.perf_counter() - start > BATCH_BUDGET:
                break
        if batch:
            first = len(self._rows)
            self.beginInsertRows(QModelIndex(), first, first + len(batch) - 1)
            for node in batch:
                node.index = len(self._groups)
                node.row = len(self._rows)
                self._groups.append(node)
                self._rows.append(node)
            self.endInsertRows()
            freeze()
            self.selection_changed.emit()
        if self._pending_pos < len(self._pending):
            self._timer.start()
        else:
            self._pending = []
            self.loading = False
            self.load_finished.emit()

    def _build(self, group: SimilarGroup) -> SimilarGroupNode:
        node = SimilarGroupNode(group)
        recs = {r.path: r for r in group.recommendations}
        for i, member in enumerate(group.members):
            m = MemberNode(member, node, i)
            path = member.entry.path
            rec = recs.get(path)
            m.suggested = rec is not None and rec.verdict is Verdict.DELETE
            m.reason = rec.reason if rec else ""
            m.override = self._overrides.get(path)
            m.protected = is_protected(path, self._protected) if self._protected else False
            node.members.append(m)
            self._by_path[path] = m
            if m.checked:
                self.selected_size += member.entry.size
                self.selected_count += 1
                self._selected.add(path)
        return node

    # -- expand / collapse ---------------------------------------------------------------

    def expand(self, node: SimilarGroupNode) -> None:
        if node.expanded or node.row < 0:
            return
        first = node.row + 1
        self.beginInsertRows(QModelIndex(), first, first + len(node.members) - 1)
        node.expanded = True
        self._rows[first:first] = node.members
        self._renumber_after(node, len(node.members))
        self.endInsertRows()

    def collapse(self, node: SimilarGroupNode) -> None:
        if not node.expanded or node.row < 0:
            return
        first = node.row + 1
        last = first + len(node.members) - 1
        self.beginRemoveRows(QModelIndex(), first, last)
        node.expanded = False
        del self._rows[first : last + 1]
        self._renumber_after(node, -len(node.members))
        self.endRemoveRows()

    def toggle_expanded(self, node: SimilarGroupNode) -> None:
        if node.expanded:
            self.collapse(node)
        else:
            self.expand(node)

    def expand_all(self) -> None:
        self._set_all_expanded(True)

    def collapse_all(self) -> None:
        self._set_all_expanded(False)

    def _set_all_expanded(self, expanded: bool) -> None:
        if all(node.expanded == expanded for node in self._groups):
            return
        self.beginResetModel()
        self._rows = []
        for node in self._groups:
            node.expanded = expanded
            node.row = len(self._rows)
            self._rows.append(node)
            if expanded:
                self._rows.extend(node.members)
        self.endResetModel()

    def _renumber_after(self, node: SimilarGroupNode, delta: int) -> None:
        for g in self._groups[node.index + 1 :]:
            g.row += delta

    # -- selection (always the user's own choices) -----------------------------------------

    def is_checked(self, path: Path) -> bool:
        node = self._by_path.get(path)
        return node is not None and node.checked

    def set_checked(self, path: Path, checked: bool) -> bool:
        node = self._by_path.get(path)
        if node is None or node.protected or node.checked == checked:
            return False
        node.override = checked
        self._live_overrides[path] = checked
        delta = 1 if checked else -1
        if checked:
            self._selected.add(path)
        else:
            self._selected.discard(path)
        self.selected_count += delta
        self.selected_size += delta * node.member.entry.size
        if node.group.expanded:
            self.dataChanged.emit(
                self.index(node.group.row + 1 + node.index, 0),
                self.index(node.group.row + 1 + node.index, len(COLUMNS) - 1),
            )
        self.selection_changed.emit()
        return True

    def selected_paths(self) -> set[Path]:
        return set(self._selected)

    def file_node(self, path: Path) -> MemberNode | None:
        return self._by_path.get(path)

    def groups(self) -> list[SimilarGroup]:
        return [g.group for g in self._groups]

    @property
    def group_count(self) -> int:
        return len(self._groups)

    @property
    def image_count(self) -> int:
        return len(self._by_path)

    def live_overrides(self) -> dict[Path, bool]:
        return dict(self._live_overrides)

    def replace_groups(self, groups: Sequence[SimilarGroup]) -> None:
        self.set_groups(groups, self._protected, dict(self._live_overrides))

    def remove_paths(self, removed: set[Path]) -> None:
        """Drop deleted images (on the calling thread: prefer ``similar_groups_after`` in a job)."""
        self.replace_groups(similar_groups_after(self.groups(), removed))

    def select_suggested(self) -> None:
        """An explicit user action: select every image the recommender would delete."""
        self._start_apply((p, n.suggested) for p, n in self._by_path.items())

    def clear_selection(self) -> None:
        self._start_apply((p, False) for p, n in self._by_path.items() if not n.protected)

    def mark_keep(self, path: Path) -> None:
        """Keep this image and select every other (unprotected) image in its group."""
        node = self._by_path.get(path)
        if node is None:
            return
        self._start_apply((m.member.entry.path, m is not node) for m in node.group.members)

    def apply_overrides(self, overrides: dict[Path, bool]) -> None:
        self._start_apply(iter(list(overrides.items())))

    def _start_apply(self, items: Iterable[tuple[Path, bool | None]]) -> None:
        self._apply_iter = iter(items)
        self._apply_timer.stop()
        self._apply_slice()

    def _apply_slice(self) -> None:
        start = time.perf_counter()
        for count, (path, value) in enumerate(self._apply_iter, 1):
            if value is None:
                self._live_overrides.pop(path, None)
            else:
                self._live_overrides[path] = value
            node = self._by_path.get(path)
            if node is not None and not node.protected:
                before = node.checked
                node.override = value
                if before != node.checked:
                    delta = 1 if node.checked else -1
                    self.selected_count += delta
                    self.selected_size += delta * node.member.entry.size
                    if node.checked:
                        self._selected.add(path)
                    else:
                        self._selected.discard(path)
            if count % 64 == 0 and time.perf_counter() - start > BATCH_BUDGET:
                self._apply_timer.start()
                break
        else:
            self._apply_iter = iter(())
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._rows) - 1, len(COLUMNS) - 1)
            )
        self.selection_changed.emit()

    def refresh_icons(self) -> None:
        self._badges = self._load_badges()
        if self._rows:
            self.dataChanged.emit(
                self.index(0, COL_SUGGEST),
                self.index(len(self._rows) - 1, COL_SUGGEST),
                [Qt.ItemDataRole.DecorationRole],
            )

    # -- QAbstractTableModel -------------------------------------------------------------

    @staticmethod
    def node_at(index: Index) -> Node | None:
        model = index.model()
        if not index.isValid() or not isinstance(model, SimilarModel):
            return None
        row = index.row()
        return model._rows[row] if 0 <= row < len(model._rows) else None

    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index: Index) -> Qt.ItemFlag:
        node = self.node_at(index)
        if node is None:
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if isinstance(node, MemberNode) and index.column() == COL_ITEM and not node.protected:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        node = self.node_at(index)
        if node is None:
            return None
        if role == NODE_ROLE:
            return node
        if isinstance(node, SimilarGroupNode):
            return self._group_data(node, index.column(), role)
        return self._member_data(node, index.column(), role)

    @staticmethod
    def _group_data(node: SimilarGroupNode, col: int, role: int) -> Any:
        g = node.group
        if role == Qt.ItemDataRole.DisplayRole and col == COL_ITEM:
            return (
                f"{len(g.members)} similar images · {g.members[0].entry.path.name}"
                f" · {human(g.reclaimable)} reclaimable"
            )
        if role == Qt.ItemDataRole.ToolTipRole and col == COL_ITEM:
            return g.id
        return None

    def _member_data(self, node: MemberNode, col: int, role: int) -> Any:
        m = node.member
        e = m.entry
        if role == Qt.ItemDataRole.CheckStateRole and col == COL_ITEM and not node.protected:
            return Qt.CheckState.Checked if node.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            return self._member_text(node, col)
        if role == Qt.ItemDataRole.DecorationRole and col == COL_SUGGEST:
            return self._badges[self.status_key(node)]
        if role == Qt.ItemDataRole.ToolTipRole and col in (COL_ITEM, COL_SUGGEST):
            return f"{e.path}\n{node.reason}"
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (
            COL_DIMS,
            COL_SIZE,
            COL_SIMILAR,
            COL_EXACT,
        ):
            return RIGHT
        return None

    @staticmethod
    def _member_text(node: MemberNode, col: int) -> str | None:
        m = node.member
        if col == COL_ITEM:
            return str(m.entry.path)
        if col == COL_DIMS:
            return f"{m.width} × {m.height}"
        if col == COL_SIZE:
            return human(m.entry.size)
        if col == COL_SIMILAR:
            return "reference" if node.index == 0 else f"{m.similarity:.0%}"
        if col == COL_EXACT:
            return f"+{len(m.aliases)}" if m.aliases else ""
        if col == COL_MODIFIED:
            return datetime.fromtimestamp(m.entry.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")
        if col == COL_SUGGEST:
            return SimilarModel.status_text(node)
        return None

    @staticmethod
    def status_key(node: MemberNode) -> str:
        if node.protected:
            return "protected"
        return "delete" if node.suggested else "keep"

    @classmethod
    def status_text(cls, node: MemberNode) -> str:
        return {"protected": "Protected", "delete": "Suggest delete", "keep": "Suggest keep"}[
            cls.status_key(node)
        ]

    def setData(self, index: Index, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        node = self.node_at(index)
        if (
            isinstance(node, MemberNode)
            and role == Qt.ItemDataRole.CheckStateRole
            and index.column() == COL_ITEM
        ):
            state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
            return self.set_checked(node.member.entry.path, state == Qt.CheckState.Checked)
        return False


class SimilarView(QTableView):
    """Space toggles the selected images, Delete asks to delete, Left/Right collapse/expand."""

    delete_requested = Signal()
    group_activated = Signal(object)  # SimilarGroup | None
    protect_folder_requested = Signal(object)  # Path

    def __init__(self, model: SimilarModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.sm = model
        self.setItemDelegate(TreeDelegate(self))
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setAlternatingRowColors(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setHighlightSections(False)
        for col, width in enumerate((460, 100, 80, 70, 90, 130)):
            self.setColumnWidth(col, width)
        self.selectionModel().currentRowChanged.connect(self._on_current_changed)

    def build_menu(self, node: Node | None) -> QMenu:
        menu = QMenu(self)
        if isinstance(node, MemberNode):
            path = node.member.entry.path
            menu.addAction(
                "Open file", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            )
            menu.addAction(
                "Open containing folder",
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))),
            )
            menu.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(str(path)))
            menu.addSeparator()
            keep = menu.addAction(
                icons.icon("keep"),
                "Keep this image and select the others",
                lambda: self.sm.mark_keep(path),
            )
            keep.setEnabled(not node.protected)
            menu.addAction(
                icons.icon("protected-folder"),
                "Mark folder as Protected",
                lambda: self.protect_folder_requested.emit(path.parent),
            )
        elif isinstance(node, SimilarGroupNode):
            label = "Collapse" if node.expanded else "Expand"
            menu.addAction(label, lambda: self.sm.toggle_expanded(node))
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        index = self.indexAt(event.pos())
        if not index.isValid():
            return
        if not self.selectionModel().isRowSelected(index.row()):
            self.selectRow(index.row())
        self.build_menu(SimilarModel.node_at(index)).exec(event.globalPos())

    def current_group(self) -> SimilarGroup | None:
        node = SimilarModel.node_at(self.currentIndex())
        if node is None:
            return None
        return node.group if isinstance(node, SimilarGroupNode) else node.group.group

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        self.group_activated.emit(self.current_group())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        node = SimilarModel.node_at(self.indexAt(event.position().toPoint()))
        if isinstance(node, SimilarGroupNode):
            self.sm.toggle_expanded(node)
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        node = SimilarModel.node_at(index)
        if (
            isinstance(node, SimilarGroupNode)
            and index.column() == COL_ITEM
            and event.position().toPoint().x() - self.columnViewportPosition(COL_ITEM) < INDENT
        ):
            self.sm.toggle_expanded(node)
            return
        super().mousePressEvent(event)

    def selected_member_nodes(self) -> list[MemberNode]:
        nodes: list[MemberNode] = []
        for index in self.selectionModel().selectedRows(COL_ITEM):
            node = SimilarModel.node_at(index)
            if isinstance(node, MemberNode):
                nodes.append(node)
            elif isinstance(node, SimilarGroupNode):
                nodes.extend(node.members)
        return nodes

    def toggle_selected(self) -> None:
        nodes = [n for n in self.selected_member_nodes() if not n.protected]
        if not nodes:
            return
        target = not all(n.checked for n in nodes)
        for n in nodes:
            self.sm.set_checked(n.member.entry.path, target)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        node = SimilarModel.node_at(self.currentIndex())
        if key == Qt.Key.Key_Space:
            self.toggle_selected()
        elif key == Qt.Key.Key_Delete:
            self.delete_requested.emit()
        elif key == Qt.Key.Key_Right and isinstance(node, SimilarGroupNode) and not node.expanded:
            self.sm.expand(node)
        elif key == Qt.Key.Key_Left and node is not None:
            group = node if isinstance(node, SimilarGroupNode) else node.group
            if group.expanded:
                self.sm.collapse(group)
                self.setCurrentIndex(self.sm.index(group.row, COL_ITEM))
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and isinstance(node, SimilarGroupNode):
            self.sm.toggle_expanded(node)
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class SimilarImagesTab(QWidget):
    """Toolbar + list + details and comparison panels."""

    delete_requested = Signal()
    folder_protected = Signal(object)  # Path
    source_changed = Signal()  # the full list changed (deletion)

    def __init__(self, runner: JobRunner | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runner = runner or JobRunner(1)
        self.model = SimilarModel(self)
        self.view = SimilarView(self.model)
        self.details = DetailsPanel()
        self.thumbnails = ThumbnailService(self)
        self.compare = ImageComparePanel(self.thumbnails)
        self.compare.bind(self.model)
        self._source: tuple[SimilarGroup, ...] = ()
        self._protected: tuple[str, ...] = ()
        self._root: Path | None = None
        self._generation = 0
        self._busy = False
        self._searched = False  # was the similar search part of the last scan?

        self.warning = QLabel(WARNING_TEXT)
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("QLabel { padding: 4px; }")
        self.expand_button = QToolButton()
        self.expand_button.setText("Expand all")
        self.collapse_button = QToolButton()
        self.collapse_button.setText("Collapse all")
        self.suggested_button = QToolButton()
        self.suggested_button.setText("Select suggested")
        self.suggested_button.setToolTip(
            "Select every image the recommender would delete (the highest resolution is kept)"
        )
        self.none_button = QToolButton()
        self.none_button.setText("Select none")
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.warning, 1)
        for button in (
            self.expand_button,
            self.collapse_button,
            self.suggested_button,
            self.none_button,
        ):
            toolbar.addWidget(button)

        self.summary = QLabel(OFF_TEXT)
        self.delete_button = QPushButton(icons.icon("move-to-trash"), "Delete selected…")
        self.delete_button.setEnabled(False)
        bar = QHBoxLayout()
        bar.addWidget(self.summary, 1)
        bar.addWidget(self.delete_button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(toolbar)
        left_layout.addWidget(self.view, 1)
        left_layout.addLayout(bar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(self.details)
        side_layout.addWidget(self.compare, 1)
        side_layout.addStretch(0)
        self.splitter.addWidget(left)
        self.splitter.addWidget(side)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)

        self.model.selection_changed.connect(self._refresh)
        self.model.load_finished.connect(self._refresh)
        self.delete_button.clicked.connect(self.delete_requested)
        self.view.delete_requested.connect(self._maybe_delete)
        self.view.protect_folder_requested.connect(self.folder_protected)
        self.view.group_activated.connect(self._on_group_activated)
        self.expand_button.clicked.connect(self.model.expand_all)
        self.collapse_button.clicked.connect(self.model.collapse_all)
        self.suggested_button.clicked.connect(self.model.select_suggested)
        self.none_button.clicked.connect(self.model.clear_selection)

    # -- data in -------------------------------------------------------------------------

    def set_groups(
        self,
        groups: Sequence[SimilarGroup],
        protected_folders: Iterable[str] = (),
        root: Path | None = None,
        searched: bool = True,
    ) -> None:
        """A new scan result. ``searched`` False: the similar search was off for that scan."""
        self._source = tuple(groups)
        self._protected = tuple(protected_folders)
        self._root = root
        self._searched = searched
        self._generation += 1
        self.compare.show_group(None)
        self.details.clear()
        self.model.set_groups(self._source, self._protected, {})
        self._refresh()

    def remove_paths(self, gone: frozenset[Path] | set[Path]) -> None:
        """Files were deleted (here or on the Duplicates tab): drop them from the full list, in
        a job, and rebuild the view keeping the user's other choices."""
        source = self._source
        overrides = self.model.live_overrides()
        self._generation += 1
        generation = self._generation

        def work(cancel: CancelToken, progress: ProgressCallback) -> list[SimilarGroup]:
            return similar_groups_after(source, gone)

        job = Job(work)

        def done(groups: list[SimilarGroup]) -> None:
            if generation == self._generation:
                self._source = tuple(groups)
                self.compare.show_group(None)
                self.source_changed.emit()
                self.model.set_groups(self._source, self._protected, overrides)

        job.signals.finished.connect(done)
        self.runner.start(job)

    def set_protected(self, protected_folders: Iterable[str]) -> None:
        """Protected folders changed: re-run the recommendations (in a job) and rebuild."""
        protected = tuple(protected_folders)
        self._protected = protected
        source, root = self._source, self._root
        overrides = self.model.live_overrides()
        self._generation += 1
        generation = self._generation

        def work(cancel: CancelToken, progress: ProgressCallback) -> list[SimilarGroup]:
            return list(recommend_similar_all(source, protected, root))

        job = Job(work)

        def done(groups: list[SimilarGroup]) -> None:
            if generation == self._generation:
                self._source = tuple(groups)
                self.model.set_groups(self._source, self._protected, overrides)

        job.signals.finished.connect(done)
        self.runner.start(job)

    @property
    def source_reclaimable(self) -> int:
        return sum(g.reclaimable for g in self._source)

    @property
    def source_group_count(self) -> int:
        return len(self._source)

    # -- misc ---------------------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def refresh_icons(self) -> None:
        self.delete_button.setIcon(icons.icon("move-to-trash"))
        self.model.refresh_icons()

    def _on_group_activated(self, group: object) -> None:
        node = SimilarModel.node_at(self.view.currentIndex())
        if isinstance(node, MemberNode):
            self.details.show_similar_member(node.member, node.group.group, node.reason)
        elif isinstance(group, SimilarGroup):
            self.details.show_similar_group(group)
        else:
            self.details.clear()
        self.compare.show_group(group if isinstance(group, SimilarGroup) else None)

    def _maybe_delete(self) -> None:
        if self.model.selected_count and not self._busy:
            self.delete_requested.emit()

    def _refresh(self) -> None:
        m = self.model
        self.delete_button.setEnabled(m.selected_count > 0 and not m.loading and not self._busy)
        if not self._searched:
            self.summary.setText(OFF_TEXT)
            return
        if not m.group_count and not m.loading:
            self.summary.setText("No similar images found.")
            return
        self.summary.setText(
            f"{m.group_count} groups · {m.selected_count} images selected "
            f"({human(m.selected_size)})" + (" · loading…" if m.loading else "")
        )
