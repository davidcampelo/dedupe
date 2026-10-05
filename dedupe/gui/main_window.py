"""Main window: folder picker, scan/cancel, progress, tabs and status bar."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.formatting import human
from dedupe.core.models import Progress, ScanResult, Stage
from dedupe.core.settings import Settings
from dedupe.gui import icons
from dedupe.gui.workers import JobRunner, ScanJob

MAX_RECENT = 10
BYTE_STAGES = {Stage.PARTIAL, Stage.FULL, Stage.COMPARE}


class MainWindow(QMainWindow):
    scan_finished = Signal(object)  # ScanResult
    scan_failed = Signal(str)

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.runner = JobRunner()
        self.job: ScanJob | None = None
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
        self.duplicates_tab = QWidget()
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

        self.choose_button.clicked.connect(self.choose_folder)
        self.recent_combo.activated.connect(self._on_recent_activated)
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.cancel_scan)

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
        job = ScanJob(self.folder, self.settings.to_scan_options())
        job.signals.progress.connect(self._on_progress)
        job.signals.finished.connect(self._on_finished)
        job.signals.failed.connect(self._on_failed)
        self.job = job
        self.error_label.hide()
        self.stage_label.setText("Starting…")
        self.progress_bar.setRange(0, 0)
        self._update_buttons()
        self.runner.start(job)

    def cancel_scan(self) -> None:
        if self.job is not None:
            self.stage_label.setText("Cancelling…")
            self.job.cancel()

    def _on_progress(self, p: Progress) -> None:
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
            if result.root and not result.files_scanned and result.skipped:
                self._show_error(f"{result.skipped[0].path}: {result.skipped[0].reason}")
        self._update_buttons()
        self.scan_finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self.job = None
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.stage_label.setText("Scan failed")
        self._show_error(message)
        self._update_buttons()
        self.scan_failed.emit(message)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _update_buttons(self) -> None:
        busy = self.busy
        self.scan_button.setEnabled(self.folder is not None and not busy)
        self.choose_button.setEnabled(not busy)
        self.recent_combo.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    # -- lifecycle -----------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        self.runner.shutdown(2000)
        super().closeEvent(event)
