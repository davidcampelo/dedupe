"""Side-by-side comparison of the copies in a group of images.

Each copy gets a card: a thumbnail (a placeholder appears at once and is swapped for the image
when its job finishes), resolution, file size, modified date, EXIF date and a Keep/Delete toggle
that is bound to the duplicates model, so the panel and the list always agree."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from dedupe.core.formatting import human
from dedupe.core.models import DuplicateGroup, FileEntry
from dedupe.gui.file_types import is_image
from dedupe.gui.preview_window import PreviewWindow
from dedupe.gui.thumbnails import THUMB_SIZE, ThumbnailService, ThumbResult, cache_key

if TYPE_CHECKING:
    from dedupe.gui.duplicates_view import DuplicatesModel

MAX_CARDS = 12


class ClickableLabel(QLabel):
    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class ImageCard(QFrame):
    def __init__(self, entry: FileEntry, content_hash: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.content_hash = content_hash
        self.key = cache_key(entry.path, THUMB_SIZE, entry.mtime_ns)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.thumb = ClickableLabel("Loading…")
        self.thumb.setFixedSize(THUMB_SIZE + 8, THUMB_SIZE + 8)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setToolTip("Double-click for a larger preview")
        self.name = QLabel(entry.path.name)
        self.name.setToolTip(str(entry.path))
        self.resolution = QLabel("Resolution: …")
        self.size_label = QLabel(f"Size: {human(entry.size)}")
        self.modified = QLabel(
            "Modified: " + datetime.fromtimestamp(entry.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")
        )
        self.exif = QLabel("EXIF date: …")
        self.delete_box = QCheckBox("Delete this copy")
        layout = QVBoxLayout(self)
        for w in (
            self.thumb,
            self.name,
            self.resolution,
            self.size_label,
            self.modified,
            self.exif,
            self.delete_box,
        ):
            layout.addWidget(w)

    def apply(self, result: ThumbResult) -> None:
        if result.image is None:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("Cannot read\nthis image")
            self.thumb.setToolTip(result.error)
            self.resolution.setText("Resolution: unknown")
            self.exif.setText("EXIF date: unknown")
            return
        self.thumb.setText("")
        self.thumb.setPixmap(QPixmap.fromImage(result.image))
        info = result.info
        self.resolution.setText(f"Resolution: {info.width} × {info.height}")
        self.exif.setText(f"EXIF date: {info.exif_date or 'none'}")


class ImageComparePanel(QWidget):
    def __init__(self, service: ThumbnailService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.model: DuplicatesModel | None = None  # set by bind()
        self.cards: list[ImageCard] = []
        self.group: DuplicateGroup | None = None
        self._syncing = False
        self._previews: list[PreviewWindow] = []
        self.heading = QLabel("")
        self.more = QLabel("")
        self._row = QHBoxLayout()
        self._row.setAlignment(Qt.AlignmentFlag.AlignLeft)
        holder = QWidget()
        holder.setLayout(self._row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        scroll.setMinimumHeight(THUMB_SIZE + 190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.heading)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.more)
        service.ready.connect(self._on_ready)
        self.hide()

    def bind(self, model: DuplicatesModel) -> None:
        """Keep the toggles in sync with a DuplicatesModel."""
        self.model = model
        model.selection_changed.connect(self.refresh_toggles)

    def show_group(self, group: DuplicateGroup | None) -> None:
        if group is not None and group is self.group:
            return
        self.group = group
        for card in self.cards:
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        if group is None or not any(is_image(f.path) for f in group.files):
            self.service.cancel_pending(set())
            self.hide()
            return
        files = group.files[:MAX_CARDS]
        wanted = {cache_key(f.path, THUMB_SIZE, f.mtime_ns) for f in files}
        self.service.cancel_pending(wanted)  # drop thumbnails for groups no longer shown
        self.heading.setText(f"Compare {len(group.files)} identical images")
        self.more.setText(
            f"… and {len(group.files) - MAX_CARDS} more copies"
            if len(group.files) > MAX_CARDS
            else ""
        )
        for f in files:
            card = ImageCard(f, group.hash)
            card.thumb.double_clicked.connect(lambda f=f: self.open_preview(f))
            card.delete_box.toggled.connect(lambda checked, p=f.path: self._on_toggled(p, checked))
            self._row.addWidget(card)
            self.cards.append(card)
            hit = self.service.request(f.path, THUMB_SIZE, f.mtime_ns, group.hash)
            if hit is not None:
                card.apply(hit)
        self.refresh_toggles()
        self.show()

    def open_preview(self, entry: FileEntry) -> PreviewWindow:
        window = PreviewWindow(
            self.service, entry.path, entry.mtime_ns, self.group.hash if self.group else "", self
        )
        window.show()
        self._previews.append(window)
        return window

    def refresh_toggles(self) -> None:
        if self.model is None:
            return
        self._syncing = True
        try:
            for card in self.cards:
                node = self.model.file_node(card.entry.path)
                card.delete_box.setEnabled(node is not None and not node.protected)
                card.delete_box.setChecked(bool(node is not None and node.checked))
                card.delete_box.setText(
                    "Protected (kept)"
                    if node is not None and node.protected
                    else "Delete this copy"
                )
        finally:
            self._syncing = False

    def _on_toggled(self, path: Path, checked: bool) -> None:
        if self._syncing or self.model is None:
            return
        self.model.set_checked(path, checked)

    def _on_ready(self, result: ThumbResult) -> None:
        for card in self.cards:
            if card.key == result.key:
                card.apply(result)
