"""Details of the selected file or group, built only from scan data (no disk access)."""

from __future__ import annotations

import stat
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QLabel, QWidget

from dedupe.core.formatting import human
from dedupe.core.models import DuplicateGroup, FileEntry


def _when(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")


class DetailsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(260)
        self._form = QFormLayout(self)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.title = QLabel("Select a file or group to see its details.")
        self.title.setWordWrap(True)
        self._form.addRow(self.title)
        self.fields: dict[str, QLabel] = {}
        for key in ("Path", "Size", "Modified", "Permissions", "Hash", "Copies", "Reason"):
            label = QLabel("")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.fields[key] = label
            self._form.addRow(f"{key}:", label)
        self.clear()

    def clear(self) -> None:
        self.title.setText("Select a file or group to see its details.")
        for label in self.fields.values():
            label.setText("")

    def show_file(self, entry: FileEntry, group: DuplicateGroup, reason: str = "") -> None:
        self.title.setText(entry.path.name)
        self.fields["Path"].setText(str(entry.path))
        self.fields["Size"].setText(f"{human(entry.size)} ({entry.size:,} bytes)")
        self.fields["Modified"].setText(_when(entry.mtime_ns))
        self.fields["Permissions"].setText(
            f"{stat.filemode(entry.mode)} ({entry.mode & 0o7777:04o})"
        )
        self.fields["Hash"].setText(group.hash)
        self.fields["Copies"].setText(str(len(group.files)))
        self.fields["Reason"].setText(reason)

    def show_group(self, group: DuplicateGroup) -> None:
        first = group.files[0]
        self.title.setText(f"Group of {len(group.files)} identical files")
        for key in ("Path", "Modified", "Permissions", "Reason"):
            self.fields[key].setText("")
        self.fields["Size"].setText(
            f"{human(group.size)} each, {human(group.reclaimable)} reclaimable"
        )
        self.fields["Hash"].setText(group.hash)
        self.fields["Copies"].setText(str(len(group.files)))
        self.fields["Path"].setText(str(first.path.parent))
