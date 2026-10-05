"""Skipped / Errors tab: files and folders that could not be read, and why."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtWidgets import QHeaderView, QLabel, QTableView, QVBoxLayout, QWidget

from dedupe.core.models import SkippedEntry

Index = QModelIndex | QPersistentModelIndex
COLUMNS = ("Path", "Reason")


class SkippedModel(QAbstractTableModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries: tuple[SkippedEntry, ...] = ()

    def set_entries(self, entries: Sequence[SkippedEntry]) -> None:
        self.beginResetModel()
        self.entries = tuple(entries)
        self.endResetModel()

    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return entry.path if index.column() == 0 else entry.reason
        return None


class SkippedTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = SkippedModel(self)
        self.summary = QLabel("Nothing was skipped.")
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setShowGrid(False)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().hide()
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.setColumnWidth(0, 620)
        self.view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.summary)
        layout.addWidget(self.view, 1)

    def set_entries(self, entries: Sequence[SkippedEntry]) -> None:
        self.model.set_entries(entries)
        n = len(entries)
        plural = "s" if n != 1 else ""
        self.summary.setText(
            "Nothing was skipped."
            if n == 0
            else f"{n} item{plural} could not be read; the scan continued without them."
        )
