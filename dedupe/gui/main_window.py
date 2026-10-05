"""Main window: folder picker, scan/cancel, progress, tabs and status bar."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QSettings, Qt, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QPalette,
)
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
from dedupe.core.hidden import HiddenItem, HiddenResult
from dedupe.core.models import DeleteMode, DuplicateGroup, Progress, ScanResult, Stage
from dedupe.core.settings import Settings, save_settings
from dedupe.gui import icons
from dedupe.gui.delete_dialog import DeleteChoice, DeleteDialog, format_summary
from dedupe.gui.duplicates_view import DuplicatesTab
from dedupe.gui.hidden_files_view import HiddenFilesTab
from dedupe.gui.settings_dialog import SettingsDialog
from dedupe.gui.skipped_view import SkippedTab
from dedupe.gui.workers import (
    ActionJob,
    ActionOutcome,
    HiddenActionJob,
    HiddenPlanJob,
    HiddenScanJob,
    Job,
    JobRunner,
    PlanJob,
    PlanOutcome,
    ScanJob,
)

MAX_RECENT = 10
BYTE_STAGES = {Stage.PARTIAL, Stage.FULL, Stage.COMPARE}


def _as_list(value: object) -> list[str]:
    """QSettings returns a bare str for one-element lists and None when unset."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class DeleteRequest:
    """What a deletion was asked for, kept while the plan is checked and the user confirms."""

    kind: str  # "duplicates" or "hidden"
    groups: list[DuplicateGroup] = field(default_factory=list)
    selection: set[Path] = field(default_factory=set)
    hidden_items: list[HiddenItem] = field(default_factory=list)
    allow_protected: tuple[Path, ...] = ()


class MainWindow(QMainWindow):
    scan_finished = Signal(object)  # ScanResult
    scan_failed = Signal(str)
    action_finished = Signal(object)  # ActionSummary

    def __init__(
        self, settings: Settings | None = None, ui_settings: QSettings | None = None
    ) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.ui_settings = ui_settings  # window state; None = do not persist (tests)
        self.runner = JobRunner()
        self.job: Job | None = None
        self.trash_backend: TrashFn | None = None  # tests inject a stub; None = send2trash
        self.log_path: Path | None = None  # None = the XDG action log
        self.settings_path: Path | None = None  # None = the XDG settings file
        self._delete_request: DeleteRequest | None = None
        self._active_kind = "duplicates"
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
        self.settings_button = QPushButton(icons.icon("settings"), "Settings…")
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(False)

        top = QHBoxLayout()
        top.addWidget(self.choose_button)
        top.addWidget(self.recent_combo, 1)
        top.addWidget(self.scan_button)
        top.addWidget(self.cancel_button)
        top.addWidget(self.settings_button)

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
        self.hidden_tab = HiddenFilesTab()
        self.skipped_tab = SkippedTab()
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
        self.hidden_tab.delete_requested.connect(self.request_hidden_delete)
        self._install_shortcuts()
        self._watch_theme()
        self._restore_ui_state()
        self.choose_button.clicked.connect(self.choose_folder)
        self.recent_combo.activated.connect(self._on_recent_activated)
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.settings_button.clicked.connect(self.open_settings)
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
        groups, selection = model.groups(), model.selected_paths()
        self._delete_request = DeleteRequest("duplicates", groups, selection)
        job = PlanJob(groups, selection, self.settings.protected_folders)
        self._begin(job, "Checking the selection…", self._on_plan_finished)

    def request_hidden_delete(self) -> None:
        model = self.hidden_tab.model
        if self.busy or model.selected_count == 0:
            return
        items = model.selected_items()
        # Items on the protected list were explicitly confirmed when they were ticked.
        allow = tuple(i.path for i in items if i.protected)
        self._delete_request = DeleteRequest("hidden", hidden_items=items, allow_protected=allow)
        job = HiddenPlanJob(items, allow)
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
        job: Job
        if request.kind == "hidden":
            job = HiddenActionJob(
                request.hidden_items,
                choice.mode,
                choice.dry_run,
                request.allow_protected,
                self.trash_backend,
                self.log_path,
            )
        else:
            job = ActionJob(
                request.groups,
                request.selection,
                choice.mode,
                choice.dry_run,
                self.settings.protected_folders,
                self.trash_backend,
                self.log_path,
            )
        self._active_kind = request.kind
        self._begin(job, "Preparing…", self._on_action_finished)

    def _on_action_finished(self, outcome: ActionOutcome) -> None:
        self._end_job()
        if outcome.summary is None:
            self.show_refusal(outcome.refused)
            return
        summary = outcome.summary
        self.stage_label.setText("Dry run finished" if summary.dry_run else "Deletion finished")
        if not summary.dry_run:
            if self._active_kind == "hidden":
                self.hidden_tab.model.remove_paths(summary.gone)
            else:
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
        """The duplicate scan finished; the hidden-file scan follows as part of the same scan."""
        self.job = None
        self.progress_bar.setRange(0, 1000)
        if result.cancelled:
            self.progress_bar.setValue(0)
            self.stage_label.setText("Cancelled")
            self._update_buttons()
            self.scan_finished.emit(result)
            return
        self.result = result
        self.progress_bar.setValue(1000)
        self.stage_label.setText(
            f"Done: {len(result.groups)} duplicate groups in {result.files_scanned} files"
        )
        self.files_label.setText(f"Files scanned: {result.files_scanned}")
        self.groups_label.setText(f"Duplicate groups: {len(result.groups)}")
        self.wasted_label.setText(f"Wasted space: {human(result.reclaimable)}")
        self.duplicates_tab.set_groups(result.groups, self.settings.protected_folders, result.root)
        self.skipped_tab.set_entries(result.skipped)
        self.hidden_tab.set_items(())
        if result.files_scanned == 0 and result.skipped:  # the folder itself was unusable
            self._show_error(f"{result.skipped[0].path}: {result.skipped[0].reason}")
            self._update_buttons()
            self.scan_finished.emit(result)
            return
        job = HiddenScanJob(result.root, self.hidden_tab.temp_patterns.isChecked())
        self._begin(job, "Looking for hidden and temporary files…", self._on_hidden_finished)

    def _on_hidden_finished(self, hidden: HiddenResult) -> None:
        self.job = None
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0 if hidden.cancelled else 1000)
        result = self.result
        assert result is not None
        if hidden.cancelled:
            self.stage_label.setText("Cancelled (hidden files not scanned)")
        else:
            self.hidden_tab.set_items(hidden.items)
            self.skipped_tab.set_entries((*result.skipped, *hidden.skipped))
            self.stage_label.setText(
                f"Done: {len(result.groups)} duplicate groups, {len(hidden.items)} hidden or "
                f"temporary items in {result.files_scanned} files"
            )
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
        self.hidden_tab.set_busy(busy)

    # -- settings -------------------------------------------------------------------------

    def open_settings(self) -> None:
        new = self.ask_settings()
        if new is not None:
            self.apply_settings(new)

    def ask_settings(self) -> Settings | None:
        """Show the settings dialog. Overridden in tests."""
        dialog = SettingsDialog(self.settings, self.runner, parent=self)
        return dialog.result_settings() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def apply_settings(self, new: Settings) -> None:
        """Adopt new settings: saved in a job, applied from the next scan on."""
        old, self.settings = self.settings, new
        path = self.settings_path
        job = Job(lambda cancel, progress: save_settings(new, path))
        job.signals.failed.connect(
            lambda msg: self.statusBar().showMessage(f"Could not save settings: {msg}", 10000)
        )
        self.runner.start(job)
        if new.protected_folders != old.protected_folders:
            self.duplicates_tab.set_protected(new.protected_folders)
        self.statusBar().showMessage("Settings saved; they apply to the next scan", 5000)

    # -- shortcuts, persisted state, theme -------------------------------------------------

    def _install_shortcuts(self) -> None:
        def add(keys: str, slot: Callable[[], None], name: str) -> None:
            action = QAction(name, self)
            action.setShortcut(QKeySequence(keys))
            action.triggered.connect(slot)
            self.addAction(action)

        add("Ctrl+O", self.choose_folder, "Choose folder")
        add("Ctrl+R", self.start_scan, "Scan")
        add("F5", self.start_scan, "Scan")
        add("Escape", self.cancel_scan, "Cancel")
        add("Ctrl+,", self.open_settings, "Settings")
        for n in range(3):
            add(f"Ctrl+{n + 1}", partial(self.tabs.setCurrentIndex, n), f"Tab {n + 1}")
        self.setTabOrder(self.choose_button, self.recent_combo)
        self.setTabOrder(self.recent_combo, self.scan_button)
        self.setTabOrder(self.scan_button, self.cancel_button)
        self.setTabOrder(self.cancel_button, self.settings_button)
        self.setTabOrder(self.settings_button, self.tabs)
        self.scan_button.setAccessibleName("Scan the chosen folder")
        self.choose_button.setAccessibleName("Choose a folder")

    def _restore_ui_state(self) -> None:
        ui = self.ui_settings
        if ui is None:
            return
        geometry = ui.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter = ui.value("duplicates/splitter")
        if splitter:
            self.duplicates_tab.splitter.restoreState(splitter)
        recent = _as_list(ui.value("folders/recent"))
        last = str(ui.value("folders/last", "") or "")
        if recent:
            self.recent = recent[:MAX_RECENT]
        if last:
            self.set_folder(Path(last))
        elif self.recent:
            self.set_folder(Path(self.recent[0]))

    def _save_ui_state(self) -> None:
        ui = self.ui_settings
        if ui is None:
            return
        ui.setValue("window/geometry", self.saveGeometry())
        ui.setValue("duplicates/splitter", self.duplicates_tab.splitter.saveState())
        ui.setValue("folders/recent", self.recent)
        ui.setValue("folders/last", str(self.folder) if self.folder else "")
        ui.sync()

    def refresh_icons(self) -> None:
        """Recolour every icon for the current palette (called when the theme changes)."""
        for button, name in (
            (self.choose_button, "choose-folder"),
            (self.scan_button, "scan-folder"),
            (self.cancel_button, "cancel-scan"),
            (self.settings_button, "settings"),
        ):
            button.setIcon(icons.icon(name))
        for i, name in enumerate(("duplicates", "hidden-files", "skipped-error")):
            self.tabs.setTabIcon(i, icons.icon(name))
        self.duplicates_tab.refresh_icons()
        self.hidden_tab.refresh_icons()

    def event(self, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.refresh_icons()
        return super().event(event)

    def _watch_theme(self) -> None:
        """Follow the application palette and the system colour scheme (widgets do not always
        receive a palette event when only the application palette changes)."""
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            # bound slots, so Qt disconnects them when this window is destroyed
            app.paletteChanged.connect(self._on_palette_changed)
            app.styleHints().colorSchemeChanged.connect(self._on_color_scheme_changed)

    @Slot(QPalette)
    def _on_palette_changed(self, _palette: QPalette) -> None:
        self.refresh_icons()

    @Slot(Qt.ColorScheme)
    def _on_color_scheme_changed(self, _scheme: Qt.ColorScheme) -> None:
        self.refresh_icons()

    # -- lifecycle -----------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_ui_state()
        self.duplicates_tab.thumbnails.shutdown(1000)
        self.runner.shutdown(2000)
        super().closeEvent(event)
