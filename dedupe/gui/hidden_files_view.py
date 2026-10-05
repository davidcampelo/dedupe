"""Hidden & Temp Files tab: dot files/folders, ~ and ~$ files, editor backups and swap files.

Only clearly disposable items start ticked. Ticking a protected item (``.ssh``, ``.bashrc`` ...)
or anything inside a top-level hidden folder of the home directory asks for an extra
confirmation first."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.formatting import human
from dedupe.core.hidden import HiddenItem
from dedupe.gui import icons

COLUMNS = ("Path", "Size", "Type", "Category", "Notes")
COL_PATH, COL_SIZE, COL_TYPE, COL_CATEGORY, COL_NOTES = range(5)

Index = QModelIndex | QPersistentModelIndex


def notes_for(item: HiddenItem) -> str:
    notes = []
    if item.protected:
        notes.append("Protected: essential configuration")
    if item.home_toplevel_dot:
        notes.append("Inside a hidden folder of your home")
    return "; ".join(notes)


class HiddenModel(QAbstractTableModel):
    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.items: tuple[HiddenItem, ...] = ()
        self._checked: set[int] = set()
        self.selected_size = 0
        # Asked before ticking a risky item; returning False leaves it unticked.
        self.confirm_risky: Any = lambda item: True

    def set_items(self, items: Sequence[HiddenItem]) -> None:
        self.beginResetModel()
        self.items = tuple(items)
        self._checked = {i for i, it in enumerate(self.items) if it.preselect}
        self.selected_size = sum(self.items[i].size for i in self._checked)
        self.endResetModel()
        self.selection_changed.emit()

    def remove_paths(self, gone: set[Any] | frozenset[Any]) -> None:
        checked_paths = {self.items[i].path for i in self._checked}
        keep = tuple(it for it in self.items if it.path not in gone)
        self.beginResetModel()
        self.items = keep
        self._checked = {i for i, it in enumerate(keep) if it.path in checked_paths}
        self.selected_size = sum(keep[i].size for i in self._checked)
        self.endResetModel()
        self.selection_changed.emit()

    @property
    def selected_count(self) -> int:
        return len(self._checked)

    def selected_items(self) -> list[HiddenItem]:
        return [self.items[i] for i in sorted(self._checked)]

    def risky_selected(self) -> bool:
        return any(
            self.items[i].protected or self.items[i].home_toplevel_dot for i in self._checked
        )

    def set_checked(self, row: int, checked: bool) -> bool:
        if not 0 <= row < len(self.items) or (row in self._checked) == checked:
            return False
        item = self.items[row]
        if checked and (item.protected or item.home_toplevel_dot) and not self.confirm_risky(item):
            return False
        if checked:
            self._checked.add(row)
            self.selected_size += item.size
        else:
            self._checked.discard(row)
            self.selected_size -= item.size
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))
        self.selection_changed.emit()
        return True

    def select_suggested(self) -> None:
        self.beginResetModel()
        self._checked = {i for i, it in enumerate(self.items) if it.preselect}
        self.selected_size = sum(self.items[i].size for i in self._checked)
        self.endResetModel()
        self.selection_changed.emit()

    def select_none(self) -> None:
        self.beginResetModel()
        self._checked = set()
        self.selected_size = 0
        self.endResetModel()
        self.selection_changed.emit()

    # -- QAbstractTableModel -------------------------------------------------------------

    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index: Index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == COL_PATH:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == COL_PATH:
            return (
                Qt.CheckState.Checked if index.row() in self._checked else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_PATH:
                return str(item.path)
            if col == COL_SIZE:
                return human(item.size)
            if col == COL_TYPE:
                return "Folder" if item.is_dir else "File"
            if col == COL_CATEGORY:
                return item.category
            if col == COL_NOTES:
                return notes_for(item)
        elif role == Qt.ItemDataRole.DecorationRole:
            if col == COL_PATH and item.protected:
                return icons.icon("protected-folder")
        elif role == Qt.ItemDataRole.TextAlignmentRole and col == COL_SIZE:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ToolTipRole and col in (COL_PATH, COL_NOTES):
            return f"{item.path}\n{notes_for(item)}".strip()
        return None

    def setData(self, index: Index, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == COL_PATH:
            state = value if isinstance(value, Qt.CheckState) else Qt.CheckState(value)
            return self.set_checked(index.row(), state == Qt.CheckState.Checked)
        return False


class HiddenFilesTab(QWidget):
    delete_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = HiddenModel(self)
        self.model.confirm_risky = self.confirm_risky
        self.intro = QLabel(
            "Hidden and temporary files. Only clearly disposable items are ticked; "
            "configuration such as .bashrc or .ssh is listed but never ticked for you."
        )
        self.intro.setWordWrap(True)
        self.temp_patterns = QCheckBox("Also match *.swp, .DS_Store, Thumbs.db, desktop.ini, *.tmp")
        self.temp_patterns.setChecked(True)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setShowGrid(False)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().hide()
        self.view.verticalHeader().setDefaultSectionSize(24)
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.view.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((480, 90, 70, 150)):
            self.view.setColumnWidth(col, width)
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.hide()
        self.total_label = QLabel("")
        self.select_suggested_button = QPushButton("Select suggested")
        self.select_none_button = QPushButton("Select none")
        self.delete_button = QPushButton(icons.icon("move-to-trash"), "Delete selected…")
        self.delete_button.setEnabled(False)
        self._busy = False

        bar = QHBoxLayout()
        bar.addWidget(self.select_suggested_button)
        bar.addWidget(self.select_none_button)
        bar.addWidget(self.total_label, 1)
        bar.addWidget(self.delete_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.intro)
        layout.addWidget(self.temp_patterns)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.warning)
        layout.addLayout(bar)

        self.model.selection_changed.connect(self._refresh)
        self.select_suggested_button.clicked.connect(self.model.select_suggested)
        self.select_none_button.clicked.connect(self.model.select_none)
        self.delete_button.clicked.connect(self.delete_requested)
        self._refresh()

    def set_items(self, items: Sequence[HiddenItem]) -> None:
        self.model.set_items(items)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def confirm_risky(self, item: HiddenItem) -> bool:
        """Extra confirmation for protected items and anything in a home dot-folder."""
        reason = (
            "It is on the list of essential configuration files and folders."
            if item.protected
            else "It is inside one of your home folder's hidden folders, where programs keep "
            "their settings and data."
        )
        answer = QMessageBox.warning(
            self,
            "Select this item?",
            f"{item.path}\n\n{reason}\nDeleting it can break programs or lose settings.\n\n"
            "Select it anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _refresh(self) -> None:
        m = self.model
        self.total_label.setText(
            f"{m.selected_count} of {len(m.items)} items selected · "
            f"{human(m.selected_size)} reclaimable"
        )
        self.delete_button.setEnabled(m.selected_count > 0 and not self._busy)
        risky = m.risky_selected()
        self.warning.setVisible(risky)
        if risky:
            self.warning.setText(
                "Warning: the selection includes protected configuration or items inside "
                "your home folder's hidden folders."
            )
