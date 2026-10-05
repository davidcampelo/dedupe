"""Details of the selected file or group, built only from scan data (no disk access)."""

from __future__ import annotations

import stat
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QLabel, QWidget

from dedupe.core.formatting import human
from dedupe.core.models import DuplicateGroup, FileEntry, SimilarGroup, SimilarMember


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
        for key in (
            "Path",
            "Size",
            "Modified",
            "Permissions",
            "Hash",
            "Copies",
            "Dimensions",
            "Similarity",
            "Reason",
        ):
            label = QLabel("")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.fields[key] = label
            self._form.addRow(f"{key}:", label)
        self.clear()

    def _show_rows(self, similar: bool) -> None:
        """Dimensions and Similarity only make sense for similar images, Hash only for exact."""
        for key, visible in (
            ("Dimensions", similar),
            ("Similarity", similar),
            ("Hash", not similar),
        ):
            self._form.setRowVisible(self.fields[key], visible)

    def clear(self) -> None:
        self.title.setText("Select a file or group to see its details.")
        for label in self.fields.values():
            label.setText("")
        self._show_rows(False)

    def show_similar_member(
        self, member: SimilarMember, group: SimilarGroup, reason: str = ""
    ) -> None:
        entry = member.entry
        self._show_rows(True)
        self.title.setText(entry.path.name)
        self.fields["Path"].setText(str(entry.path))
        self.fields["Size"].setText(f"{human(entry.size)} ({entry.size:,} bytes)")
        self.fields["Modified"].setText(_when(entry.mtime_ns))
        self.fields["Permissions"].setText(
            f"{stat.filemode(entry.mode)} ({entry.mode & 0o7777:04o})"
        )
        self.fields["Copies"].setText(
            f"{len(group.members)} similar images"
            + (f", plus {len(member.aliases)} exact copies of this one" if member.aliases else "")
        )
        self.fields["Dimensions"].setText(f"{member.width} × {member.height}")
        self.fields["Similarity"].setText(
            "reference image" if member is group.members[0] else f"{member.similarity:.0%}"
        )
        self.fields["Reason"].setText(reason)

    def show_similar_group(self, group: SimilarGroup) -> None:
        self._show_rows(True)
        self.title.setText(f"Group of {len(group.members)} similar images")
        for key in ("Path", "Modified", "Permissions", "Dimensions", "Similarity", "Reason"):
            self.fields[key].setText("")
        self.fields["Size"].setText(f"{human(group.reclaimable)} reclaimable")
        self.fields["Copies"].setText(str(len(group.members)))
        self.fields["Path"].setText(str(group.members[0].entry.path.parent))

    def show_file(self, entry: FileEntry, group: DuplicateGroup, reason: str = "") -> None:
        self._show_rows(False)
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
        self._show_rows(False)
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
