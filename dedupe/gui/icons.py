"""Theme-aware icon loader. Monochrome SVGs use ``currentColor`` and are recoloured from the
palette; keep/delete/protected/warning icons and the badges keep fixed semantic colours."""

from __future__ import annotations

from importlib import resources

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

APP_ICON = "io.github.davidcampelo.Dedupe"

ICON_NAMES = (
    "scan-folder", "choose-folder", "duplicates", "hidden-files", "temp-tilde",
    "compare-images", "keep", "move-to-trash", "delete-forever", "hard-link",
    "protected-folder", "dry-run", "verify-hash", "space-freed", "cancel-scan",
    "settings", "action-log", "skipped-error",
)  # fmt: skip
BADGE_NAMES = (
    "badge-keep", "badge-delete", "badge-protected", "badge-hardlinked", "badge-changed",
)  # fmt: skip

SIZES = (16, 24, 32, 48)


def svg_text(name: str) -> str:
    return (resources.files("dedupe.gui") / "resources" / "icons" / f"{name}.svg").read_text(
        "utf-8"
    )


def _render(svg: str, size: int, dpr: float = 1.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    px = int(size * dpr)
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    pm = QPixmap.fromImage(image)
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, palette: QPalette | None = None) -> QIcon:
    """An icon recoloured for ``palette`` (normal and disabled states)."""
    if palette is None:
        palette = QPalette()
    svg = svg_text(name)
    result = QIcon()
    active = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)
    disabled = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    states = ((QIcon.Mode.Normal, active), (QIcon.Mode.Disabled, disabled))
    for mode, color in states:
        tinted = svg.replace("currentColor", color.name(QColor.NameFormat.HexRgb))
        for size in SIZES:
            result.addPixmap(_render(tinted, size, 2.0), mode)
    return result


def app_icon() -> QIcon:
    result = QIcon()
    svg = svg_text(APP_ICON)
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(_render(svg, size))
    return result
