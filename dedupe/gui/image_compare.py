"""Side-by-side comparison of the copies in a group of images.

Each copy gets a card: a thumbnail (a placeholder appears at once and is swapped for the image
when its job finishes), resolution, file size, modified date, EXIF date and a Keep/Delete toggle
that is bound to the duplicates (or similar images) model, so the panel and the list always
agree. For a group of *similar* images a card also shows the match percentage and how many exact
copies the image stands for."""

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
from dedupe.core.models import DuplicateGroup, FileEntry, SimilarGroup, SimilarMember
from dedupe.gui.file_types import is_image
from dedupe.gui.preview_window import PreviewWindow
from dedupe.gui.thumbnails import THUMB_SIZE, ThumbnailService, ThumbResult, cache_key

if TYPE_CHECKING:
    from dedupe.gui.duplicates_view import DuplicatesModel
    from dedupe.gui.similar_view import SimilarModel

MAX_CARDS = 12


class ClickableLabel(QLabel):
    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class ImageCard(QFrame):
    def __init__(
        self,
        entry: FileEntry,
        content_hash: str,
        parent: QWidget | None = None,
        member: SimilarMember | None = None,
        reference: bool = False,
    ) -> None:
        super().__init__(parent)
        self.entry = entry
        self.member = member
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
        self.delete_box = QCheckBox("Delete this image" if member else "Delete this copy")
        self.match = QLabel("")
        self.exact = QLabel("")
        layout = QVBoxLayout(self)
        widgets: list[QWidget] = [
            self.thumb,
            self.name,
            self.resolution,
            self.size_label,
            self.modified,
        ]
        if member is not None:
            self.resolution.setText(f"Resolution: {member.width} × {member.height}")
            self.match.setText(
                "Reference image" if reference else f"{member.similarity:.0%} similar"
            )
            widgets.append(self.match)
            if member.aliases:
                n = len(member.aliases)
                self.exact.setText(f"+{n} exact cop{'y' if n == 1 else 'ies'}")
                self.exact.setToolTip("\n".join(str(a) for a in member.aliases))
                widgets.append(self.exact)
        widgets += [self.exif, self.delete_box]
        for w in widgets:
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
        self.model: DuplicatesModel | SimilarModel | None = None  # set by bind()
        self.cards: list[ImageCard] = []
        self.group: DuplicateGroup | SimilarGroup | None = None
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

    def bind(self, model: DuplicatesModel | SimilarModel) -> None:
        """Keep the toggles in sync with a DuplicatesModel or SimilarModel."""
        self.model = model
        model.selection_changed.connect(self.refresh_toggles)

    def show_group(self, group: DuplicateGroup | SimilarGroup | None) -> None:
        if group is not None and group is self.group:
            return
        self.group = group
        for card in self.cards:
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        if isinstance(group, SimilarGroup):
            entries = [m.entry for m in group.members]
            key, noun = group.id, "similar images"
        elif group is not None:
            entries = list(group.files)
            key, noun = group.hash, "identical images"
        else:
            entries, key, noun = [], "", ""
        if not any(is_image(e.path) for e in entries):
            self.service.cancel_pending(set())
            self.hide()
            return
        shown = entries[:MAX_CARDS]
        wanted = {cache_key(e.path, THUMB_SIZE, e.mtime_ns) for e in shown}
        self.service.cancel_pending(wanted)  # drop thumbnails for groups no longer shown
        self.heading.setText(f"Compare {len(entries)} {noun}")
        self.more.setText(
            f"… and {len(entries) - MAX_CARDS} more "
            + ("images" if isinstance(group, SimilarGroup) else "copies")
            if len(entries) > MAX_CARDS
            else ""
        )
        # Similar members have no content hash: their thumbnails skip the by-hash disk cache.
        content_hash = "" if isinstance(group, SimilarGroup) else key
        for n, e in enumerate(shown):
            member = group.members[n] if isinstance(group, SimilarGroup) else None
            card = ImageCard(e, content_hash, member=member, reference=n == 0)
            card.thumb.double_clicked.connect(lambda e=e: self.open_preview(e))
            card.delete_box.toggled.connect(lambda checked, p=e.path: self._on_toggled(p, checked))
            self._row.addWidget(card)
            self.cards.append(card)
            hit = self.service.request(e.path, THUMB_SIZE, e.mtime_ns, content_hash)
            if hit is not None:
                card.apply(hit)
        self.refresh_toggles()
        self.show()

    def open_preview(self, entry: FileEntry) -> PreviewWindow:
        content_hash = self.group.hash if isinstance(self.group, DuplicateGroup) else ""
        window = PreviewWindow(self.service, entry.path, entry.mtime_ns, content_hash, self)
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
                    else ("Delete this image" if card.member else "Delete this copy")
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
