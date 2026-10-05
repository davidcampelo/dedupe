"""Delete confirmation dialog, plus plain-text summaries of plans and results.

Nothing is deleted unless the user accepts this dialog. Permanent deletion additionally needs
the "cannot be recovered" checkbox, which is cleared whenever the mode changes."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.actions import ActionPlan, ActionSummary, Status
from dedupe.core.formatting import human
from dedupe.core.models import DeleteMode

SHOWN_PATHS = 20
ACK_TEXT = "I understand these files cannot be recovered"
OK_TEXT = {
    DeleteMode.TRASH: "Move to Trash",
    DeleteMode.PERMANENT: "Delete permanently",
    DeleteMode.HARDLINK: "Replace with hard links",
}


@dataclass(frozen=True, slots=True)
class DeleteChoice:
    mode: DeleteMode
    dry_run: bool


class DeleteDialog(QDialog):
    def __init__(
        self,
        plan: ActionPlan,
        default_mode: DeleteMode = DeleteMode.TRASH,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Delete selected files")
        self.plan = plan
        self.setMinimumWidth(560)

        count = len(plan.items)
        self.summary_label = QLabel(
            f"<b>{count} file{'s' if count != 1 else ''}</b> selected, {human(plan.total_size)} "
            "to reclaim. One copy of every group is always kept."
        )
        self.paths_view = QPlainTextEdit()
        self.paths_view.setReadOnly(True)
        lines = [str(i.entry.path) for i in plan.items[:SHOWN_PATHS]]
        if count > SHOWN_PATHS:
            lines.append(f"… and {count - SHOWN_PATHS} more")
        self.paths_view.setPlainText("\n".join(lines))
        self.paths_view.setMaximumHeight(160)

        self.trash_radio = QRadioButton("Move to Trash (recoverable)")
        self.permanent_radio = QRadioButton("Delete permanently")
        self.hardlink_radio = QRadioButton("Replace with hard links (keeps every path working)")
        self.hardlink_radio.setEnabled(plan.hardlink_possible)
        if not plan.hardlink_possible:
            self.hardlink_radio.setToolTip(
                plan.hardlink_reason or "Not possible for this selection"
            )
        self.ack_box = QCheckBox(ACK_TEXT)
        self.ack_box.setVisible(False)
        self.dry_run_box = QCheckBox("Dry run: show what would happen, change nothing")

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.paths_view)
        for w in (self.trash_radio, self.permanent_radio, self.hardlink_radio, self.ack_box):
            layout.addWidget(w)
        layout.addWidget(self.dry_run_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button: QPushButton = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(self.buttons)

        initial = default_mode
        if initial is DeleteMode.HARDLINK and not plan.hardlink_possible:
            initial = DeleteMode.TRASH
        {
            DeleteMode.TRASH: self.trash_radio,
            DeleteMode.PERMANENT: self.permanent_radio,
            DeleteMode.HARDLINK: self.hardlink_radio,
        }[initial].setChecked(True)

        for radio in (self.trash_radio, self.permanent_radio, self.hardlink_radio):
            radio.toggled.connect(self._on_mode_changed)
        self.ack_box.toggled.connect(self._refresh)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._on_mode_changed()

    @property
    def mode(self) -> DeleteMode:
        if self.permanent_radio.isChecked():
            return DeleteMode.PERMANENT
        if self.hardlink_radio.isChecked():
            return DeleteMode.HARDLINK
        return DeleteMode.TRASH

    @property
    def choice(self) -> DeleteChoice:
        return DeleteChoice(self.mode, self.dry_run_box.isChecked())

    def _on_mode_changed(self) -> None:
        self.ack_box.blockSignals(True)
        self.ack_box.setChecked(False)  # a new mode always needs a fresh acknowledgement
        self.ack_box.blockSignals(False)
        self.ack_box.setVisible(self.mode is DeleteMode.PERMANENT)
        self.ok_button.setText(OK_TEXT[self.mode])
        self._refresh()

    def _refresh(self) -> None:
        self.ok_button.setEnabled(self.mode is not DeleteMode.PERMANENT or self.ack_box.isChecked())

    def accept(self) -> None:
        if self.ok_button.isEnabled():  # Enter must not bypass the acknowledgement
            super().accept()


def format_summary(summary: ActionSummary) -> str:
    verb = {
        DeleteMode.TRASH: "moved to Trash",
        DeleteMode.PERMANENT: "deleted permanently",
        DeleteMode.HARDLINK: "replaced with hard links",
    }[summary.mode]
    lines: list[str] = []
    if summary.dry_run:
        lines.append("Dry run: nothing was changed.")
        lines.append(f"{summary.done} files would be {verb}, freeing {human(summary.freed)}.")
    else:
        lines.append(f"{summary.done} files {verb}, freeing {human(summary.freed)}.")
    for status in (
        Status.CHANGED,
        Status.KEEPER_CHANGED,
        Status.FAILED,
        Status.ALREADY_LINKED,
        Status.NOT_RUN,
    ):
        items = [r for r in summary.results if r.status is status]
        if not items:
            continue
        lines.append(f"\n{len(items)} skipped or failed ({status.value}):")
        for r in items[:10]:
            lines.append(f"  {r.path}" + (f": {r.detail}" if r.detail else ""))
        if len(items) > 10:
            lines.append(f"  … and {len(items) - 10} more")
    lines.extend(f"\nWarning: {w}" for w in summary.warnings)
    return "\n".join(lines)
