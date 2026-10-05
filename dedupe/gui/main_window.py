"""Main window: folder picker, scan/cancel, progress, tabs and status bar."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.actions import ActionPlan, TrashFn
from dedupe.core.formatting import human
from dedupe.core.models import DeleteMode, DuplicateGroup, Progress, ScanResult, Stage
from dedupe.core.settings import Settings, save_settings
from dedupe.gui import icons
from dedupe.gui.delete_dialog import DeleteChoice, DeleteDialog, format_summary
from dedupe.gui.duplicates_view import DuplicatesTab
from dedupe.gui.workers import (
    ActionJob,
    ActionOutcome,
    Job,
    JobRunner,
    PlanJob,
    PlanOutcome,
    ScanJob,
)

MAX_RECENT = 10
BYTE_STAGES = {Stage.PARTIAL, Stage.FULL, Stage.COMPARE}


class MainWindow(QMainWindow):
    scan_finished = Signal(object)  # ScanResult
    scan_failed = Signal(str)
    action_finished = Signal(object)  # ActionSummary

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.runner = JobRunner()
        self.job: Job | None = None
        self.trash_backend: TrashFn | None = None  # tests inject a stub; None = send2trash
        self.log_path: Path | None = None  # None = the XDG action log
        self.settings_path: Path | None = None  # None = the XDG settings file
        self._delete_request: tuple[list[DuplicateGroup], set[Path]] | None = None
        self.result: ScanResult | None = None
        self.folder: Path | None = None
        self.recent: list[str] = []

        self.setWindowTitle("Dedupe")
        self.setWindowIcon(icons.app_icon())
        self.setAcceptDrops(True)
        self.resize(1100, 720)

        self.choose_button = QPushButton(icons.icon("choose-folder"), "Choose folder…")
        self.recent_combo = QComboBox()
        self.recent_combo.setMinimumWidth(320)
        self.recent_combo.setPlaceholderText("No folder selected (drop one here)")
        self.scan_button = QPushButton(icons.icon("scan-folder"), "Scan")
        self.cancel_button = QPushButton(icons.icon("cancel-scan"), "Cancel")
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(False)

        top = QHBoxLayout()
        top.addWidget(self.choose_button)
        top.addWidget(self.recent_combo, 1)
        top.addWidget(self.scan_button)
        top.addWidget(self.cancel_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.stage_label = QLabel("Idle")
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        self.tabs = QTabWidget()
        self.duplicates_tab = DuplicatesTab(self.runner)
        self.hidden_tab = QWidget()
        self.skipped_tab = QWidget()
        self.tabs.addTab(self.duplicates_tab, icons.icon("duplicates"), "Duplicates")
        self.tabs.addTab(self.hidden_tab, icons.icon("hidden-files"), "Hidden && Temp Files")
        self.tabs.addTab(self.skipped_tab, icons.icon("skipped-error"), "Skipped / Errors")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.error_label)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.files_label = QLabel("Files scanned: 0")
        self.groups_label = QLabel("Duplicate groups: 0")
        self.wasted_label = QLabel("Wasted space: 0 B")
        self.selected_label = QLabel("Selected for deletion: 0 B")
        for label in (self.files_label, self.groups_label, self.wasted_label, self.selected_label):
            self.statusBar().addPermanentWidget(label)

        self.duplicates_tab.model.selection_changed.connect(self._update_selected_label)
        self.duplicates_tab.source_changed.connect(self._update_totals)
        self.duplicates_tab.folder_protected.connect(self._on_folder_protected)
        self.choose_button.clicked.connect(self.choose_folder)
        self.recent_combo.activated.connect(self._on_recent_activated)
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.duplicates_tab.delete_requested.connect(self.request_delete)

    # -- folder selection ----------------------------------------------------------------

    def choose_folder(self) -> None:
        start = str(self.folder) if self.folder else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder to scan", start)
        if chosen:
            self.set_folder(Path(chosen))

    def set_folder(self, folder: Path) -> None:
        """Select a folder. It is validated by the scan job, never by the GUI thread."""
        self.folder = folder
        text = str(folder)
        self.recent = [text, *[r for r in self.recent if r != text]][:MAX_RECENT]
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItems(self.recent)
        self.recent_combo.setCurrentIndex(0)
        self.recent_combo.blockSignals(False)
        self._update_buttons()

    def _on_recent_activated(self, index: int) -> None:
        self.set_folder(Path(self.recent_combo.itemText(index)))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and not self.busy:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.set_folder(Path(url.toLocalFile()))
                event.acceptProposedAction()
                return

    # -- scanning ------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.job is not None

    def start_scan(self) -> None:
        if self.folder is None or self.busy:
            return
        self.error_label.hide()
        self._begin(
            ScanJob(self.folder, self.settings.to_scan_options()), "Starting…", self._on_finished
        )

    def _begin(self, job: Job, text: str, on_finished: Callable[[Any], None]) -> None:
        job.signals.progress.connect(self._on_progress)
        job.signals.finished.connect(on_finished)
        job.signals.failed.connect(self._on_failed)
        self.job = job
        self.stage_label.setText(text)
        self.progress_bar.setRange(0, 0)
        self._update_buttons()
        self.runner.start(job)

    # -- deleting ------------------------------------------------------------------------

    def request_delete(self) -> None:
        model = self.duplicates_tab.model
        if self.busy or model.loading or model.selected_count == 0:
            return
        self._delete_request = (model.groups(), model.selected_paths())
        groups, selection = self._delete_request
        job = PlanJob(groups, selection, self.settings.protected_folders)
        self._begin(job, "Checking the selection…", self._on_plan_finished)

    def _on_plan_finished(self, outcome: PlanOutcome) -> None:
        self._end_job()
        request, self._delete_request = self._delete_request, None
        if outcome.plan is None or request is None:
            self.show_refusal(outcome.refused)
            return
        choice = self.confirm_delete(outcome.plan)
        if choice is None:
            self.stage_label.setText("Deletion cancelled; nothing was changed")
            return
        groups, selection = request
        job = ActionJob(
            groups,
            selection,
            choice.mode,
            choice.dry_run,
            self.settings.protected_folders,
            self.trash_backend,
            self.log_path,
        )
        self._begin(job, "Preparing…", self._on_action_finished)

    def _on_action_finished(self, outcome: ActionOutcome) -> None:
        self._end_job()
        if outcome.summary is None:
            self.show_refusal(outcome.refused)
            return
        summary = outcome.summary
        self.stage_label.setText("Dry run finished" if summary.dry_run else "Deletion finished")
        if not summary.dry_run:
            self.duplicates_tab.remove_paths(summary.gone)
        self.show_summary(format_summary(summary))
        self.action_finished.emit(summary)

    def confirm_delete(self, plan: ActionPlan) -> DeleteChoice | None:
        """Ask the user. Overridden in tests; the real dialog is modal."""
        dialog = DeleteDialog(plan, DeleteMode(self.settings.default_delete_mode), self)
        return dialog.choice if dialog.exec() == QDialog.DialogCode.Accepted else None

    def show_refusal(self, reasons: Sequence[str]) -> None:
        text = "\n".join(reasons[:12]) + (
            f"\n… and {len(reasons) - 12} more" if len(reasons) > 12 else ""
        )
        self.stage_label.setText("Deletion refused; nothing was changed")
        QMessageBox.warning(self, "Cannot delete this selection", text)

    def show_summary(self, text: str) -> None:
        QMessageBox.information(self, "Done", text)

    def cancel_scan(self) -> None:
        if self.job is not None:
            self.stage_label.setText("Cancelling…")
            self.job.cancel()

    def _on_progress(self, p: Progress) -> None:
        if p.stage is Stage.ACTION:
            self.stage_label.setText(f"Working: {p.done} / {p.total} files  {p.current_path}")
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(1000 * p.done / p.total) if p.total else 0)
            return
        if p.stage in BYTE_STAGES:
            text = f"{p.stage.value}: {human(p.done)} / {human(p.total)}"
        else:
            text = f"{p.stage.value}: {p.done} files"
        if p.current_path:
            text += f"  {p.current_path}"
        self.stage_label.setText(text)
        if p.total > 0 and p.stage in BYTE_STAGES:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(1000 * p.done / p.total))
        else:
            self.progress_bar.setRange(0, 0)

    def _end_job(self) -> None:
        self.job = None
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self._update_buttons()

    def _on_finished(self, result: ScanResult) -> None:
        self.job = None
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0 if result.cancelled else 1000)
        if result.cancelled:
            self.stage_label.setText("Cancelled")
        else:
            self.result = result
            self.stage_label.setText(
                f"Done: {len(result.groups)} duplicate groups in {result.files_scanned} files"
            )
            self.files_label.setText(f"Files scanned: {result.files_scanned}")
            self.groups_label.setText(f"Duplicate groups: {len(result.groups)}")
            self.wasted_label.setText(f"Wasted space: {human(result.reclaimable)}")
            self.duplicates_tab.set_groups(
                result.groups, self.settings.protected_folders, result.root
            )
            if result.root and not result.files_scanned and result.skipped:
                self._show_error(f"{result.skipped[0].path}: {result.skipped[0].reason}")
        self._update_buttons()
        self.scan_finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self.job = None
        self._delete_request = None
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.stage_label.setText("Operation failed")
        self._show_error(message)
        self._update_buttons()
        self.scan_failed.emit(message)

    def _update_totals(self) -> None:
        tab = self.duplicates_tab
        self.groups_label.setText(f"Duplicate groups: {tab.source_group_count}")
        self.wasted_label.setText(f"Wasted space: {human(tab.source_reclaimable)}")

    def _on_folder_protected(self, folder: Path) -> None:
        """Remember the folder as protected (saved in a job), then re-run the recommendations."""
        self.settings = self.settings.with_protected_folder(str(folder))
        settings = self.settings
        path = self.settings_path
        self.runner.start(Job(lambda cancel, progress: save_settings(settings, path)))
        self.duplicates_tab.set_protected(settings.protected_folders)
        self.statusBar().showMessage(f"{folder} is now protected", 5000)

    def _update_selected_label(self) -> None:
        size = self.duplicates_tab.model.selected_size
        self.selected_label.setText(f"Selected for deletion: {human(size)}")

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _update_buttons(self) -> None:
        busy = self.busy
        self.scan_button.setEnabled(self.folder is not None and not busy)
        self.choose_button.setEnabled(not busy)
        self.recent_combo.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.duplicates_tab.set_busy(busy)

    # -- lifecycle -----------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        self.runner.shutdown(2000)
        super().closeEvent(event)
