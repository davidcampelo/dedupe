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
from collections.abc import Iterable, Sequence
from datetime import datetime
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
    Signal,
)
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.formatting import human
from dedupe.core.models import DuplicateGroup, FileEntry, Verdict
from dedupe.core.recommender import is_protected
from dedupe.gui import icons
from dedupe.gui.gcutil import freeze

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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._groups: list[GroupNode] = []
        self._rows: list[Node] = []
        self._by_path: dict[Path, FileNode] = {}
        self._pending: list[DuplicateGroup] = []
        self._pending_pos = 0
        self._protected: tuple[str, ...] = ()
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
        self, groups: Sequence[DuplicateGroup], protected_folders: Iterable[str] = ()
    ) -> None:
        """Replace the contents; rows arrive in batches (see ``load_finished``)."""
        self._timer.stop()
        self.beginResetModel()
        self._discard_nodes()
        self._groups = []
        self._rows = []
        self._by_path = {}
        self.selected_size = 0
        self.selected_count = 0
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
        """Break node <-> group reference cycles so refcounting frees them even though the
        collector never scans frozen objects (see gcutil)."""
        for g in self._groups:
            g.files.clear()
        self._by_path = {}
        self._rows = []

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
            f.protected = is_protected(entry.path, self._protected) if self._protected else False
            node.files.append(f)
            self._by_path[entry.path] = f
            if f.checked:
                self.selected_size += entry.size
                self.selected_count += 1
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
        delta = 1 if checked else -1
        self.selected_count += delta
        self.selected_size += delta * node.entry.size
        if node.group.expanded:
            self.dataChanged.emit(self.index(node.row, COL_ITEM), self.index(node.row, COL_STATUS))
        self.selection_changed.emit()
        return True

    def selected_paths(self) -> set[Path]:
        return {p for p, n in self._by_path.items() if n.checked}

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

    def select_all_suggested(self) -> None:
        """Reset every choice to the recommendation."""
        for node in self._by_path.values():
            node.override = None
        self._recount()

    def clear_selection(self) -> None:
        for node in self._by_path.values():
            if not node.protected:
                node.override = False
        self._recount()

    def _recount(self) -> None:
        nodes = [n for n in self._by_path.values() if n.checked]
        self.selected_count = len(nodes)
        self.selected_size = sum(n.entry.size for n in nodes)
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


class DuplicatesTab(QWidget):
    delete_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = DuplicatesModel(self)
        self.view = DuplicatesView(self.model)
        self.summary = QLabel("Scan a folder to find duplicates.")
        self.delete_button = QPushButton(icons.icon("move-to-trash"), "Delete selected…")
        self.delete_button.setEnabled(False)
        bar = QHBoxLayout()
        bar.addWidget(self.summary, 1)
        bar.addWidget(self.delete_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view, 1)
        layout.addLayout(bar)
        self.model.selection_changed.connect(self._refresh)
        self.delete_button.clicked.connect(self.delete_requested)
        self.view.delete_requested.connect(self._maybe_delete)

    def set_groups(
        self, groups: Sequence[DuplicateGroup], protected_folders: Iterable[str] = ()
    ) -> None:
        self.model.set_groups(groups, protected_folders)

    def _maybe_delete(self) -> None:
        if self.model.selected_count:
            self.delete_requested.emit()

    def _refresh(self) -> None:
        m = self.model
        self.delete_button.setEnabled(m.selected_count > 0 and not m.loading)
        if m.group_count or m.loading:
            self.summary.setText(
                f"{m.group_count} groups · {m.selected_count} files selected "
                f"({human(m.selected_size)})" + (" · loading…" if m.loading else "")
            )
