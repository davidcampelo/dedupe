"""A larger preview of one image, opened by double-clicking a thumbnail."""

from __future__ import annotations

import contextlib
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QScrollArea, QVBoxLayout, QWidget

from dedupe.gui.thumbnails import PREVIEW_SIZE, ThumbnailService, ThumbResult, cache_key


class PreviewWindow(QDialog):
    def __init__(
        self,
        service: ThumbnailService,
        path: Path,
        mtime_ns: int,
        content_hash: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(path.name)
        self.resize(900, 700)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._key = cache_key(path, PREVIEW_SIZE, mtime_ns)
        self.label = QLabel("Loading…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption = QLabel(str(path))
        self.caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.label)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.caption)
        self._service = service
        service.ready.connect(self._on_ready)
        hit = service.request(path, PREVIEW_SIZE, mtime_ns, content_hash)
        if hit is not None:
            self._show(hit)

    def _on_ready(self, result: ThumbResult) -> None:
        if result.key == self._key:
            self._show(result)

    def _show(self, result: ThumbResult) -> None:
        if result.image is None:
            self.label.setText(f"Cannot preview this image\n{result.error}")
            return
        self.label.setPixmap(QPixmap.fromImage(result.image))
        info = result.info
        if info.width:
            self.caption.setText(f"{self.caption.text()}  ·  {info.width} × {info.height}")

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        with contextlib.suppress(RuntimeError, TypeError):
            self._service.ready.disconnect(self._on_ready)
        super().closeEvent(event)
