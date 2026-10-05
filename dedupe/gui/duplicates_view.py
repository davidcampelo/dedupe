"""Duplicates tab: a grouped, expandable list (group rows, file rows with a "delete" checkbox).

It is a flat table model that emulates a tree (expand/collapse inserts and removes the file
rows of one group). A QTreeView lays out *every* row through Python model calls on each change
(about 12 us per row, so ~400 ms for 30k groups); a table view only touches visible rows.

The model inserts results in small batches from the event loop (each batch <= ~12 ms of work)
so loading 100k files never blocks the GUI. The recommendation (Keep/Delete) and the user's
overrides are stored separately, so recommendations can be re-run without losing choices.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
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
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.actions import groups_after
from dedupe.core.formatting import human
from dedupe.core.models import (
    CancelToken,
    DuplicateGroup,
    FileEntry,
    ProgressCallback,
    Verdict,
)
from dedupe.core.recommender import (
    is_protected,
    keep_in_folder,
    keep_newest,
    recommend_all,
)
from dedupe.gui import icons
from dedupe.gui.details_panel import DetailsPanel
from dedupe.gui.file_types import CATEGORIES
from dedupe.gui.gcutil import freeze
from dedupe.gui.image_compare import ImageComparePanel
from dedupe.gui.image_grid import ImageGridView, image_groups
from dedupe.gui.thumbnails import ThumbnailService
from dedupe.gui.view_options import SortKey, ViewOptions, select_groups
from dedupe.gui.workers import Job, JobRunner

COLUMNS = ("Group / File", "Size", "Copies", "Reclaimable", "Modified", "Status", "Reason")
COL_ITEM, COL_SIZE, COL_COPIES, COL_RECLAIM, COL_MODIFIED, COL_STATUS, COL_REASON = range(7)

NODE_ROLE = Qt.ItemDataRole.UserRole + 1
BATCH_BUDGET = 0.012  # seconds of node building per event-loop turn
ROW_HEIGHT = 24
INDENT = 26  # pixels file rows are indented under their group

Index = QModelIndex | QPersistentModelIndex


class FileNode:
    __slots__ = ("entry", "group", "index", "override", "protected", "reason", "recommended")

    def __init__(self, entry: FileEntry, group: GroupNode, index: int) -> None:
        self.entry = entry
        self.group = group
        self.index = index  # position inside the group
        self.recommended = False  # True = suggested for deletion
        self.override: bool | None = None  # the user's explicit choice, if any
        self.protected = False
        self.reason = ""

    @property
    def checked(self) -> bool:
        if self.protected:
            return False
        return self.recommended if self.override is None else self.override

    @property
    def row(self) -> int:
        """Flat row of this file; only meaningful while its group is expanded."""
        return self.group.row + 1 + self.index


class GroupNode:
    __slots__ = ("expanded", "files", "group", "index", "row")

    def __init__(self, group: DuplicateGroup) -> None:
        self.group = group
        self.index = -1  # position among the groups
        self.row = -1  # flat row of the group header
        self.expanded = False
        self.files: list[FileNode] = []


Node = FileNode | GroupNode


class DuplicatesModel(QAbstractTableModel):
    selection_changed = Signal()
    load_finished = Signal()
    overrides_applied = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._groups: list[GroupNode] = []
        self._rows: list[Node] = []
        self._by_path: dict[Path, FileNode] = {}
        self._pending: list[DuplicateGroup] = []
        self._pending_pos = 0
        self._protected: tuple[str, ...] = ()
        self._overrides: dict[Path, bool] = {}  # carried into the next load
        self._live_overrides: dict[Path, bool] = {}  # the user's current explicit choices
        self._selected: set[Path] = set()
        self._graveyard: list[list[GroupNode]] = []
        self._apply_iter: Iterator[tuple[Path, bool | None]] = iter(())
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(0)
        self._apply_timer.timeout.connect(self._apply_slice)
        self._reaper = QTimer(self)
        self._reaper.setSingleShot(True)
        self._reaper.setInterval(0)
        self._reaper.timeout.connect(self._reap)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._load_batch)
        self.selected_size = 0
        self.selected_count = 0
        self.loading = False
        self._badges = {
            "keep": icons.icon("badge-keep"),
            "delete": icons.icon("badge-delete"),
            "protected": icons.icon("badge-protected"),
        }

    # -- loading -------------------------------------------------------------------------

    def set_groups(
        self,
        groups: Sequence[DuplicateGroup],
        protected_folders: Iterable[str] = (),
        overrides: dict[Path, bool] | None = None,
    ) -> None:
        """Replace the contents; rows arrive in batches (see ``load_finished``).
        ``overrides`` carries the user's explicit choices over a reload."""
        self._timer.stop()
        self._apply_timer.stop()
        self._apply_iter = iter(())
        self._overrides = overrides or {}
        self._live_overrides = dict(self._overrides)
        self.beginResetModel()
        self._discard_nodes()
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

    def _discard_nodes(self) -> None:
        """Retire the old nodes. Their node <-> group reference cycles are broken in small
        slices from the event loop (see ``_reap``): freeing 100k nodes at once takes ~100 ms,
        and frozen objects (gcutil) would otherwise never be collected."""
        if self._groups:
            self._graveyard.append(self._groups)
            self._reaper.start()
        self._by_path = {}
        self._rows = []

    def _reap(self) -> None:
        start = time.perf_counter()
        while self._graveyard and time.perf_counter() - start < BATCH_BUDGET:
            groups = self._graveyard[-1]
            for _ in range(200):
                if not groups:
                    break
                groups.pop().files.clear()
            if not groups:
                self._graveyard.pop()
        if self._graveyard:
            self._reaper.start()

    def _load_batch(self) -> None:
        start = time.perf_counter()
        batch: list[GroupNode] = []
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
            freeze()  # new nodes never need a cycle scan; keeps gen-2 GC pauses off the GUI
            self.selection_changed.emit()
        if self._pending_pos < len(self._pending):
            self._timer.start()
        else:
            self._pending = []
            self.loading = False
            self.load_finished.emit()

    def _build(self, group: DuplicateGroup) -> GroupNode:
        node = GroupNode(group)
        recs = {r.path: r for r in group.recommendations}
        for i, entry in enumerate(group.files):
            f = FileNode(entry, node, i)
            rec = recs.get(entry.path)
            f.recommended = rec is not None and rec.verdict is Verdict.DELETE
            f.reason = rec.reason if rec else ""
            f.override = self._overrides.get(entry.path)
            f.protected = is_protected(entry.path, self._protected) if self._protected else False
            node.files.append(f)
            self._by_path[entry.path] = f
            if f.checked:
                self.selected_size += entry.size
                self.selected_count += 1
                self._selected.add(entry.path)
        return node

    # -- expand / collapse ---------------------------------------------------------------

    def expand(self, node: GroupNode) -> None:
        if node.expanded or node.row < 0:
            return
        first = node.row + 1
        self.beginInsertRows(QModelIndex(), first, first + len(node.files) - 1)
        node.expanded = True
        self._rows[first:first] = node.files
        self._renumber_after(node, len(node.files))
        self.endInsertRows()

    def collapse(self, node: GroupNode) -> None:
        if not node.expanded or node.row < 0:
            return
        first = node.row + 1
        last = first + len(node.files) - 1
        self.beginRemoveRows(QModelIndex(), first, last)
        node.expanded = False
        del self._rows[first : last + 1]
        self._renumber_after(node, -len(node.files))
        self.endRemoveRows()

    def toggle_expanded(self, node: GroupNode) -> None:
        if node.expanded:
            self.collapse(node)
        else:
            self.expand(node)

    def expand_all(self) -> None:
        for node in list(self._groups):
            self.expand(node)

    def _renumber_after(self, node: GroupNode, delta: int) -> None:
        for g in self._groups[node.index + 1 :]:
            g.row += delta

    # -- selection -----------------------------------------------------------------------

    def is_checked(self, path: Path) -> bool:
        node = self._by_path.get(path)
        return node is not None and node.checked

    def set_checked(self, path: Path, checked: bool) -> bool:
        node = self._by_path.get(path)
        if node is None or node.protected or node.checked == checked:
            return False
        node.override = checked
        self._live_overrides[path] = checked
        if checked:
            self._selected.add(path)
        else:
            self._selected.discard(path)
        delta = 1 if checked else -1
        self.selected_count += delta
        self.selected_size += delta * node.entry.size
        if node.group.expanded:
            self.dataChanged.emit(self.index(node.row, COL_ITEM), self.index(node.row, COL_STATUS))
        self.selection_changed.emit()
        return True

    def selected_paths(self) -> set[Path]:
        return set(self._selected)

    def group_of(self, path: Path) -> DuplicateGroup | None:
        node = self._by_path.get(path)
        return node.group.group if node else None

    def file_node(self, path: Path) -> FileNode | None:
        return self._by_path.get(path)

    def groups(self) -> list[DuplicateGroup]:
        return [g.group for g in self._groups]

    @property
    def group_count(self) -> int:
        return len(self._groups)

    @property
    def file_count(self) -> int:
        return len(self._by_path)

    def replace_groups(self, groups: Sequence[DuplicateGroup]) -> None:
        """Reload with new groups (e.g. after a deletion), keeping the user's choices."""
        self.set_groups(groups, self._protected, dict(self._live_overrides))

    def remove_paths(self, removed: set[Path]) -> None:
        """Drop deleted files; a group left with fewer than two copies is no longer a duplicate
        group. Computed on the calling thread: prefer ``actions.groups_after`` in a job."""
        self.replace_groups(groups_after(self.groups(), removed))

    def reclaimable(self) -> int:
        return sum(g.group.reclaimable for g in self._groups)

    def refresh_icons(self) -> None:
        self._badges = {
            "keep": icons.icon("badge-keep"),
            "delete": icons.icon("badge-delete"),
            "protected": icons.icon("badge-protected"),
        }
        if self._rows:
            self.dataChanged.emit(
                self.index(0, COL_STATUS),
                self.index(len(self._rows) - 1, COL_STATUS),
                [Qt.ItemDataRole.DecorationRole],
            )

    def live_overrides(self) -> dict[Path, bool]:
        return dict(self._live_overrides)

    def select_all_suggested(self) -> None:
        """Reset every choice to the recommendation."""
        self._live_overrides = {}
        self._start_apply((p, None) for p in self._by_path)

    def clear_selection(self) -> None:
        self._start_apply(((p, False) for p, n in self._by_path.items() if not n.protected))

    def apply_overrides(self, overrides: dict[Path, bool]) -> None:
        """Apply many explicit choices; large batches are applied in time slices."""
        self._start_apply(iter(list(overrides.items())))

    def mark_keep(self, path: Path) -> None:
        """Keep this copy and mark every other (unprotected) copy in its group for deletion."""
        node = self._by_path.get(path)
        if node is None:
            return
        changes: list[tuple[Path, bool | None]] = []
        for f in node.group.files:
            changes.append((f.entry.path, f is not node))
        self._start_apply(iter(changes))

    def _start_apply(self, items: Iterator[tuple[Path, bool | None]]) -> None:
        self._apply_iter = items
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
                after = node.checked
                if before != after:
                    delta = 1 if after else -1
                    self.selected_count += delta
                    self.selected_size += delta * node.entry.size
                    if after:
                        self._selected.add(path)
                    else:
                        self._selected.discard(path)
            if count % 64 == 0 and time.perf_counter() - start > BATCH_BUDGET:
                self._apply_timer.start()
                break
        else:
            self._apply_iter = iter(())
            self.overrides_applied.emit()
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._rows) - 1, len(COLUMNS) - 1)
            )
        self.selection_changed.emit()

    # -- QAbstractTableModel -------------------------------------------------------------

    @staticmethod
    def node_at(index: Index) -> Node | None:
        model = index.model()
        if not index.isValid() or not isinstance(model, DuplicatesModel):
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
        if isinstance(node, FileNode) and index.column() == COL_ITEM and not node.protected:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        node = self.node_at(index)
        if node is None:
            return None
        if role == NODE_ROLE:
            return node
        col = index.column()
        if isinstance(node, GroupNode):
            return self._group_data(node, col, role)
        return self._file_data(node, col, role)

    def _group_data(self, node: GroupNode, col: int, role: int) -> Any:
        g = node.group
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_ITEM:
                return f"{g.hash[:8]}  {g.files[0].path.name}"
            if col == COL_SIZE:
                return human(g.size)
            if col == COL_COPIES:
                return str(len(g.files))
            if col == COL_RECLAIM:
                return human(g.reclaimable)
        elif role == Qt.ItemDataRole.ToolTipRole and col == COL_ITEM:
            return g.hash
        elif role == Qt.ItemDataRole.TextAlignmentRole and col in (
            COL_SIZE,
            COL_COPIES,
            COL_RECLAIM,
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _file_data(self, node: FileNode, col: int, role: int) -> Any:
        e = node.entry
        if role == Qt.ItemDataRole.CheckStateRole and col == COL_ITEM and not node.protected:
            return Qt.CheckState.Checked if node.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_ITEM:
                return str(e.path)
            if col == COL_SIZE:
                return human(e.size)
            if col == COL_MODIFIED:
                return datetime.fromtimestamp(e.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")
            if col == COL_STATUS:
                return self.status_text(node)
            if col == COL_REASON:
                return node.reason
        elif role == Qt.ItemDataRole.DecorationRole and col == COL_STATUS:
            return self._badges[self.status_key(node)]
        elif role == Qt.ItemDataRole.ToolTipRole and col in (COL_ITEM, COL_REASON):
            return f"{e.path}\n{node.reason}"
        elif role == Qt.ItemDataRole.TextAlignmentRole and col == COL_SIZE:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def status_key(node: FileNode) -> str:
        if node.protected:
            return "protected"
        return "delete" if node.checked else "keep"

    @classmethod
    def status_text(cls, node: FileNode) -> str:
        return {"protected": "Protected", "delete": "Delete", "keep": "Keep"}[cls.status_key(node)]

    def setData(self, index: Index, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        node = self.node_at(index)
        if (
            isinstance(node, FileNode)
            and role == Qt.ItemDataRole.CheckStateRole
            and index.column() == COL_ITEM
        ):
            state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
            return self.set_checked(node.entry.path, state == Qt.CheckState.Checked)
        return False


class TreeDelegate(QStyledItemDelegate):
    """Draws the tree look in column 0: a branch arrow on group rows, an indent on file rows."""

    def _inset(self, option: QStyleOptionViewItem, index: Index) -> QStyleOptionViewItem:
        opt = QStyleOptionViewItem(option)
        if index.column() == COL_ITEM:
            opt.rect = opt.rect.adjusted(INDENT, 0, 0, 0)
        return opt

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Index) -> None:
        node = DuplicatesModel.node_at(index)
        if index.column() == COL_ITEM and isinstance(node, GroupNode):
            opt = QStyleOptionViewItem(option)
            opt.rect = opt.rect.adjusted(INDENT, 0, 0, 0)
            opt.font.setBold(True)
            super().paint(painter, opt, index)
            arrow = QStyleOptionViewItem(option)
            arrow.rect = QRect(option.rect.left(), option.rect.top(), INDENT, option.rect.height())
            arrow.state |= QStyle.StateFlag.State_Children
            if node.expanded:
                arrow.state |= QStyle.StateFlag.State_Open
            style = option.widget.style() if option.widget else None
            if style is not None:
                style.drawPrimitive(
                    QStyle.PrimitiveElement.PE_IndicatorBranch, arrow, painter, option.widget
                )
            return
        if index.column() == COL_ITEM:
            super().paint(painter, self._inset(option, index), index)
            return
        super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index):  # type: ignore[no-untyped-def]
        if index.column() == COL_ITEM:
            option = self._inset(option, index)
        return super().editorEvent(event, model, option, index)


class DuplicatesView(QTableView):
    """Keyboard: Space toggles the selected files, Delete asks to delete, Left/Right (or
    Enter) collapse/expand a group."""

    delete_requested = Signal()
    group_activated = Signal(object)  # DuplicateGroup | None, when the current row changes
    protect_folder_requested = Signal(object)  # Path

    def __init__(self, model: DuplicatesModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.dm = model
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
        for col, width in enumerate((520, 90, 60, 100, 130, 100)):
            self.setColumnWidth(col, width)
        self.selectionModel().currentRowChanged.connect(self._on_current_changed)

    def build_menu(self, node: Node | None) -> QMenu:
        """The right-click menu for a row (also used directly by tests)."""
        menu = QMenu(self)
        if isinstance(node, FileNode):
            path = node.entry.path
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
                icons.icon("keep"), "Mark as Keep", lambda: self.dm.mark_keep(path)
            )
            keep.setEnabled(not node.protected)
            menu.addAction(
                icons.icon("protected-folder"),
                "Mark folder as Protected",
                lambda: self.protect_folder_requested.emit(path.parent),
            )
        elif isinstance(node, GroupNode):
            label = "Collapse" if node.expanded else "Expand"
            menu.addAction(label, lambda: self.dm.toggle_expanded(node))
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        index = self.indexAt(event.pos())
        if not index.isValid():
            return
        if not self.selectionModel().isRowSelected(index.row()):
            self.selectRow(index.row())
        self.build_menu(DuplicatesModel.node_at(index)).exec(event.globalPos())

    def current_group(self) -> DuplicateGroup | None:
        node = DuplicatesModel.node_at(self.currentIndex())
        if node is None:
            return None
        return node.group if isinstance(node, GroupNode) else node.group.group

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        self.group_activated.emit(self.current_group())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        node = DuplicatesModel.node_at(self.indexAt(event.position().toPoint()))
        if isinstance(node, GroupNode):
            self.dm.toggle_expanded(node)
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        node = DuplicatesModel.node_at(index)
        if (
            isinstance(node, GroupNode)
            and index.column() == COL_ITEM
            and event.position().toPoint().x() - self.columnViewportPosition(COL_ITEM) < INDENT
        ):
            self.dm.toggle_expanded(node)
            return
        super().mousePressEvent(event)

    def selected_file_nodes(self) -> list[FileNode]:
        nodes: list[FileNode] = []
        for index in self.selectionModel().selectedRows(COL_ITEM):
            node = DuplicatesModel.node_at(index)
            if isinstance(node, FileNode):
                nodes.append(node)
            elif isinstance(node, GroupNode):
                nodes.extend(node.files)
        return nodes

    def toggle_selected(self) -> None:
        nodes = [n for n in self.selected_file_nodes() if not n.protected]
        if not nodes:
            return
        target = not all(n.checked for n in nodes)  # mixed or unchecked -> check all
        for n in nodes:
            self.dm.set_checked(n.entry.path, target)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        node = DuplicatesModel.node_at(self.currentIndex())
        if key == Qt.Key.Key_Space:
            self.toggle_selected()
        elif key == Qt.Key.Key_Delete:
            self.delete_requested.emit()
        elif key == Qt.Key.Key_Right and isinstance(node, GroupNode) and not node.expanded:
            self.dm.expand(node)
        elif key == Qt.Key.Key_Left and node is not None:
            group = node if isinstance(node, GroupNode) else node.group
            if group.expanded:
                self.dm.collapse(group)
                self.setCurrentIndex(self.dm.index(group.row, COL_ITEM))
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and isinstance(node, GroupNode):
            self.dm.toggle_expanded(node)
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class BulkRule(StrEnum):
    SUGGESTED = "Select all suggested"
    NEWEST = "Keep the newest copy in every group"
    FOLDER = "Keep copies in a folder…"
    NONE = "Deselect everything"


class DuplicatesTab(QWidget):
    """Filter bar + list + details panel. ``set_groups`` takes the full scan result; sort and
    filters rebuild the visible list in a job (never on the GUI thread)."""

    delete_requested = Signal()
    folder_protected = Signal(object)  # Path: the user asked to protect this folder
    view_changed = Signal()  # the visible list was rebuilt (sort or filter applied)
    source_changed = Signal()  # the full list changed (deletion)
    grid_changed = Signal()  # the grid tiles were rebuilt

    FILTER_DEBOUNCE_MS = 200

    def __init__(self, runner: JobRunner | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runner = runner or JobRunner(1)
        self.model = DuplicatesModel(self)
        self.view = DuplicatesView(self.model)
        self.details = DetailsPanel()
        self.thumbnails = ThumbnailService(self)
        self.compare = ImageComparePanel(self.thumbnails)
        self.compare.bind(self.model)
        self._source: tuple[DuplicateGroup, ...] = ()
        self._protected: tuple[str, ...] = ()
        self._root: Path | None = None
        self._options = ViewOptions()
        self._generation = 0
        self._busy = False
        self._grid_generation = 0

        self.sort_combo = QComboBox()
        for key in SortKey:
            self.sort_combo.addItem(key.value, key)
        self.type_combo = QComboBox()
        self.type_combo.addItem("All file types", None)
        for category in CATEGORIES:
            self.type_combo.addItem(category, category)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by path…")
        self.filter_edit.setClearButtonEnabled(True)
        self.grid_button = QToolButton()
        self.grid_button.setText("Grid")
        self.grid_button.setIcon(icons.icon("compare-images"))
        self.grid_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.grid_button.setCheckable(True)
        self.grid_button.setToolTip("Browse image groups as a grid of thumbnails")
        self.bulk_button = QToolButton()
        self.bulk_button.setText("Bulk rules")
        self.bulk_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.bulk_menu = QMenu(self.bulk_button)
        self.bulk_actions = {rule: self.bulk_menu.addAction(rule.value) for rule in BulkRule}
        self.bulk_button.setMenu(self.bulk_menu)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Sort by"))
        filters.addWidget(self.sort_combo)
        filters.addWidget(self.type_combo)
        filters.addWidget(self.filter_edit, 1)
        filters.addWidget(self.grid_button)
        filters.addWidget(self.bulk_button)

        self.summary = QLabel("Scan a folder to find duplicates.")
        self.delete_button = QPushButton(icons.icon("move-to-trash"), "Delete selected…")
        self.delete_button.setEnabled(False)
        bar = QHBoxLayout()
        bar.addWidget(self.summary, 1)
        bar.addWidget(self.delete_button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(filters)
        self.grid = ImageGridView(self.thumbnails)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.grid)
        left_layout.addWidget(self.stack, 1)
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

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.FILTER_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._options_changed)
        self.filter_edit.textChanged.connect(lambda _: self._debounce.start())
        self.sort_combo.currentIndexChanged.connect(lambda _: self._options_changed())
        self.type_combo.currentIndexChanged.connect(lambda _: self._options_changed())
        for rule, action in self.bulk_actions.items():
            action.triggered.connect(lambda _=False, r=rule: self.apply_rule(r))

        self.model.selection_changed.connect(self._refresh)
        self.delete_button.clicked.connect(self.delete_requested)
        self.view.delete_requested.connect(self._maybe_delete)
        self.view.protect_folder_requested.connect(self.folder_protected)
        self.view.group_activated.connect(self._on_group_activated)
        self.grid_button.toggled.connect(self._set_grid_mode)
        self.grid.group_selected.connect(self._on_grid_selected)
        self.model.load_finished.connect(self._refresh_grid)

    # -- data in -------------------------------------------------------------------------

    def set_groups(
        self,
        groups: Sequence[DuplicateGroup],
        protected_folders: Iterable[str] = (),
        root: Path | None = None,
    ) -> None:
        """A new scan result: every choice starts from the recommendations again."""
        self._source = tuple(groups)
        self._protected = tuple(protected_folders)
        self._root = root
        self._rebuild_view({})

    def replace_groups(self, groups: Sequence[DuplicateGroup]) -> None:
        """Groups after a deletion; the user's other choices are kept."""
        self._source = tuple(groups)
        self._rebuild_view(self.model.live_overrides())

    def remove_paths(self, gone: frozenset[Path] | set[Path]) -> None:
        """Files were deleted: drop them from the full list (not just the visible one), in a
        job, and rebuild the view. A group left with one copy is no longer a duplicate group."""
        source = self._source
        overrides = self.model.live_overrides()
        self._generation += 1
        generation = self._generation

        def work(cancel: CancelToken, progress: ProgressCallback) -> list[DuplicateGroup]:
            return groups_after(source, gone)

        job = Job(work)

        def done(groups: list[DuplicateGroup]) -> None:
            if generation == self._generation:
                self._source = tuple(groups)
                self.source_changed.emit()
                self._rebuild_view(overrides)

        job.signals.finished.connect(done)
        self.runner.start(job)

    @property
    def source_reclaimable(self) -> int:
        return sum(g.reclaimable for g in self._source)

    @property
    def source_group_count(self) -> int:
        return len(self._source)

    def set_protected(self, protected_folders: Iterable[str]) -> None:
        """Protected folders changed: re-run the recommendations for every group (in a job)
        and rebuild the view, keeping the user's explicit choices."""
        protected = tuple(protected_folders)
        self._protected = protected
        source, root = self._source, self._root
        overrides = self.model.live_overrides()
        self._generation += 1
        generation = self._generation

        def work(cancel: CancelToken, progress: ProgressCallback) -> list[DuplicateGroup]:
            return list(recommend_all(source, protected, root))

        job = Job(work)

        def done(groups: list[DuplicateGroup]) -> None:
            if generation == self._generation:
                self._source = tuple(groups)
                self._rebuild_view(overrides)

        job.signals.finished.connect(done)
        self.runner.start(job)

    def options(self) -> ViewOptions:
        return self._options

    # -- view rebuilding -------------------------------------------------------------------

    def _options_changed(self) -> None:
        self._debounce.stop()
        self._options = ViewOptions(
            SortKey(self.sort_combo.currentData()),
            self.type_combo.currentData(),
            self.filter_edit.text(),
        )
        self._rebuild_view(self.model.live_overrides())

    def _rebuild_view(self, overrides: dict[Path, bool]) -> None:
        self._generation += 1
        generation = self._generation
        if self._options.is_default:
            self.model.set_groups(self._source, self._protected, overrides)
            self.view_changed.emit()
            return
        source, options = self._source, self._options

        def work(cancel: CancelToken, progress: ProgressCallback) -> list[DuplicateGroup]:
            return select_groups(source, options)

        job = Job(work)

        def done(groups: list[DuplicateGroup]) -> None:
            if generation == self._generation:  # a newer request supersedes this one
                self.model.set_groups(groups, self._protected, overrides)
                self.view_changed.emit()

        job.signals.finished.connect(done)
        self.runner.start(job)

    # -- bulk rules --------------------------------------------------------------------------

    def apply_rule(self, rule: BulkRule, folder: str | None = None) -> None:
        if rule is BulkRule.SUGGESTED:
            self.model.select_all_suggested()
            return
        if rule is BulkRule.NONE:
            self.model.clear_selection()
            return
        if rule is BulkRule.FOLDER and folder is None:
            folder = QFileDialog.getExistingDirectory(self, "Keep copies in this folder")
            if not folder:
                return
        groups, protected, root = self.model.groups(), self._protected, self._root

        def work(cancel: CancelToken, progress: ProgressCallback) -> dict[Path, bool]:
            out: dict[Path, bool] = {}
            for n, g in enumerate(groups):
                if n % 256 == 0:
                    time.sleep(0.0001)
                if rule is BulkRule.NEWEST:
                    recs = keep_newest(g, protected, root)
                else:
                    assert folder is not None
                    recs = keep_in_folder(g, folder, protected, root)
                for r in recs:
                    out[r.path] = r.verdict is Verdict.DELETE
            return out

        job = Job(work)
        job.signals.finished.connect(self.model.apply_overrides)
        self.runner.start(job)

    def refresh_icons(self) -> None:
        self.delete_button.setIcon(icons.icon("move-to-trash"))
        self.grid_button.setIcon(icons.icon("compare-images"))
        self.model.refresh_icons()

    # -- grid mode ----------------------------------------------------------------------------

    @property
    def grid_mode(self) -> bool:
        return self.stack.currentWidget() is self.grid

    def _set_grid_mode(self, on: bool) -> None:
        current = self.view.current_group() if not self.grid_mode else self.grid.selected_group()
        if on:
            self.stack.setCurrentWidget(self.grid)
            self._refresh_grid(select=current)
        else:
            self.stack.setCurrentWidget(self.view)
            self.select_group(current)

    def _refresh_grid(self, select: DuplicateGroup | None = None) -> None:
        """Rebuild the tiles from the visible groups (a job: it scans every group)."""
        if not self.grid_mode:
            return
        groups = self.model.groups()
        selected = select if isinstance(select, DuplicateGroup) else self.grid.selected_group()
        self._grid_generation += 1
        generation = self._grid_generation

        def work(cancel: CancelToken, progress: ProgressCallback) -> list[DuplicateGroup]:
            return image_groups(groups)

        job = Job(work)

        def done(result: list[DuplicateGroup]) -> None:
            if generation != self._grid_generation or not self.grid_mode:
                return
            self.grid.set_groups(result)
            if selected is not None:
                self.grid.select_group(selected)
            self.grid_changed.emit()

        job.signals.finished.connect(done)
        self.runner.start(job)

    def _on_grid_selected(self, group: object) -> None:
        if isinstance(group, DuplicateGroup):
            self.details.show_group(group)
        self.compare.show_group(group if isinstance(group, DuplicateGroup) else None)

    def select_group(self, group: DuplicateGroup | None) -> bool:
        """Select a group's header row in the list (expanding nothing)."""
        if group is None:
            return False
        for node in self.model._groups:
            if node.group is group:
                index = self.model.index(node.row, COL_ITEM)
                self.view.setCurrentIndex(index)
                self.view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                return True
        return False

    # -- misc ---------------------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def _on_group_activated(self, group: object) -> None:
        node = DuplicatesModel.node_at(self.view.currentIndex())
        if isinstance(node, FileNode):
            self.details.show_file(node.entry, node.group.group, node.reason)
        elif isinstance(group, DuplicateGroup):
            self.details.show_group(group)
        else:
            self.details.clear()
        self.compare.show_group(group if isinstance(group, DuplicateGroup) else None)

    def _maybe_delete(self) -> None:
        if self.model.selected_count and not self._busy:
            self.delete_requested.emit()

    def _refresh(self) -> None:
        m = self.model
        self.delete_button.setEnabled(m.selected_count > 0 and not m.loading and not self._busy)
        if m.group_count or m.loading or self._source:
            shown = (
                f"{m.group_count} of {len(self._source)} groups"
                if m.group_count != len(self._source)
                else f"{m.group_count} groups"
            )
            self.summary.setText(
                f"{shown} · {m.selected_count} files selected ({human(m.selected_size)})"
                + (" · loading…" if m.loading else "")
            )
