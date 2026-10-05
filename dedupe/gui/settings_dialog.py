"""Settings dialog: every setting from the spec, backed by core.settings.Settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.cache import HashCache
from dedupe.core.models import (
    SIMILARITY_PRESETS,
    CancelToken,
    DeleteMode,
    ProgressCallback,
)
from dedupe.core.perceptual import similar_available
from dedupe.core.settings import Settings
from dedupe.gui.workers import Job, JobRunner

MODE_LABELS = {
    DeleteMode.TRASH: "Move to Trash (recoverable)",
    DeleteMode.PERMANENT: "Delete permanently",
    DeleteMode.HARDLINK: "Replace with hard links",
}


def clear_default_cache() -> int:
    cache = HashCache()
    try:
        return cache.clear()
    finally:
        cache.close()


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        runner: JobRunner | None = None,
        clear_cache_fn: Callable[[], int] = clear_default_cache,
        parent: QWidget | None = None,
        similar_available_fn: Callable[[], bool] = similar_available,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._base = settings
        self._runner = runner or JobRunner(1)
        self._clear_cache_fn = clear_cache_fn

        self.exclude_edit = QPlainTextEdit("\n".join(settings.exclude))
        self.exclude_edit.setToolTip(
            "One pattern per line: a name (.git), a glob (*.tmp) or an absolute folder (/proc)"
        )
        self.exclude_edit.setMaximumHeight(110)
        self.protected_list = QListWidget()
        self.protected_list.addItems(settings.protected_folders)
        self.protected_list.setMaximumHeight(100)
        self.add_protected = QPushButton("Add folder…")
        self.remove_protected = QPushButton("Remove")
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 2_000_000_000)
        self.min_size.setSuffix(" bytes")
        self.min_size.setGroupSeparatorShown(True)
        self.min_size.setValue(settings.min_size)
        self.follow_symlinks = QCheckBox("Follow symbolic links (loops are detected)")
        self.follow_symlinks.setChecked(settings.follow_symlinks)
        self.cross_fs = QCheckBox("Cross filesystem boundaries")
        self.cross_fs.setChecked(settings.cross_filesystems)
        self.include_hidden = QCheckBox("Include hidden files in the duplicate scan")
        self.include_hidden.setChecked(settings.include_hidden)
        self.paranoid = QCheckBox("Paranoid mode: compare bytes inside each group")
        self.paranoid.setChecked(settings.paranoid)
        self.use_cache = QCheckBox("Use the hash cache (makes re-scans fast)")
        self.use_cache.setChecked(settings.use_cache)
        self.workers = QSpinBox()
        self.workers.setRange(0, 64)
        self.workers.setSpecialValueText("Automatic")
        self.workers.setValue(settings.workers)
        self.delete_mode = QComboBox()
        for mode, label in MODE_LABELS.items():
            self.delete_mode.addItem(label, mode.value)
        self.delete_mode.setCurrentIndex(self.delete_mode.findData(settings.default_delete_mode))
        self.similar_images = QCheckBox("Find similar images (slower: every image is decoded)")
        self.similar_images.setChecked(settings.similar_images)
        self.similarity = QComboBox()
        for name, bits in SIMILARITY_PRESETS.items():
            self.similarity.addItem(f"{name.capitalize()} ({bits} bits)", bits)
        if self.similarity.findData(settings.similarity_threshold) < 0:  # set by hand in the file
            self.similarity.addItem(
                f"Custom ({settings.similarity_threshold} bits)", settings.similarity_threshold
            )
        self.similarity.setCurrentIndex(self.similarity.findData(settings.similarity_threshold))
        self.similarity.setToolTip(
            "How different two images may be and still count as similar. Strict finds only "
            "near-identical copies; Loose also finds heavier edits (and more false matches)."
        )
        self.similar_available = similar_available_fn()
        self.similar_hint = QLabel("")
        if not self.similar_available:
            self.similar_images.setEnabled(False)
            self.similarity.setEnabled(False)
            self.similar_hint.setText("Install the optional extra to enable this: dedupe[similar]")
        self.clear_cache = QPushButton("Clear hash cache")
        self.clear_cache_status = QLabel("")

        protected_buttons = QHBoxLayout()
        protected_buttons.addWidget(self.add_protected)
        protected_buttons.addWidget(self.remove_protected)
        protected_buttons.addStretch(1)
        cache_row = QHBoxLayout()
        cache_row.addWidget(self.clear_cache)
        cache_row.addWidget(self.clear_cache_status, 1)

        form = QFormLayout()
        form.addRow("Exclusions", self.exclude_edit)
        form.addRow("Protected folders", self.protected_list)
        form.addRow("", protected_buttons)
        form.addRow("Minimum file size", self.min_size)
        form.addRow("Worker threads", self.workers)
        form.addRow("Default delete mode", self.delete_mode)
        form.addRow(self.follow_symlinks)
        form.addRow(self.cross_fs)
        form.addRow(self.include_hidden)
        form.addRow(self.paranoid)
        form.addRow(self.use_cache)
        form.addRow(self.similar_images)
        form.addRow("Similarity", self.similarity)
        form.addRow("", self.similar_hint)
        form.addRow("Cache", cache_row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

        self.add_protected.clicked.connect(self._add_protected)
        self.remove_protected.clicked.connect(self._remove_protected)
        self.clear_cache.clicked.connect(self._clear_cache)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    # -- result ---------------------------------------------------------------------------

    def result_settings(self) -> Settings:
        patterns = tuple(
            line.strip() for line in self.exclude_edit.toPlainText().splitlines() if line.strip()
        )
        protected = tuple(
            self.protected_list.item(i).text() for i in range(self.protected_list.count())
        )
        similar_images = self._base.similar_images
        threshold = self._base.similarity_threshold
        if self.similar_available:  # otherwise the controls are disabled: keep the file's values
            similar_images = self.similar_images.isChecked()
            threshold = int(self.similarity.currentData())
        return replace(
            self._base,
            similar_images=similar_images,
            similarity_threshold=threshold,
            exclude=patterns,
            protected_folders=protected,
            min_size=self.min_size.value(),
            follow_symlinks=self.follow_symlinks.isChecked(),
            cross_filesystems=self.cross_fs.isChecked(),
            include_hidden=self.include_hidden.isChecked(),
            paranoid=self.paranoid.isChecked(),
            workers=self.workers.value(),
            default_delete_mode=str(self.delete_mode.currentData()),
            use_cache=self.use_cache.isChecked(),
        )

    # -- actions --------------------------------------------------------------------------

    def _add_protected(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Protect this folder")
        if folder:
            self.add_protected_folder(folder)

    def add_protected_folder(self, folder: str) -> None:
        existing = {self.protected_list.item(i).text() for i in range(self.protected_list.count())}
        if folder not in existing:
            self.protected_list.addItem(folder)

    def _remove_protected(self) -> None:
        for item in self.protected_list.selectedItems():
            self.protected_list.takeItem(self.protected_list.row(item))

    def _clear_cache(self) -> None:
        """Runs as a Job: only this button shows busy, the dialog stays responsive."""
        self.clear_cache.setEnabled(False)
        self.clear_cache.setText("Clearing…")
        fn = self._clear_cache_fn

        def work(cancel: CancelToken, progress: ProgressCallback) -> int:
            return fn()

        job = Job(work)
        job.signals.finished.connect(self._cache_cleared)
        job.signals.failed.connect(self._cache_clear_failed)
        self._runner.start(job)

    def _cache_cleared(self, count: int) -> None:
        self.clear_cache.setEnabled(True)
        self.clear_cache.setText("Clear hash cache")
        self.clear_cache_status.setText(f"Cleared {count} cached hashes")

    def _cache_clear_failed(self, message: str) -> None:
        self.clear_cache.setEnabled(True)
        self.clear_cache.setText("Clear hash cache")
        self.clear_cache_status.setText(f"Could not clear the cache: {message}")
