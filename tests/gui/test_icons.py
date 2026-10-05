from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QIcon, QPalette
from pytestqt.qtbot import QtBot

from dedupe.gui import icons


def palette(bg: str, fg: str) -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(bg))
    p.setColor(QPalette.ColorRole.WindowText, QColor(fg))
    return p


LIGHT = palette("#f4f6f5", "#101a1f")
DARK = palette("#1c2024", "#e6ebe9")


def luminance(c: QColor) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())


def contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def ink(ic: QIcon) -> QColor:
    """Most common opaque colour in the icon's 48 px rendering."""
    image = ic.pixmap(48, 48).toImage()
    counts: dict[int, int] = {}
    for y in range(image.height()):
        for x in range(image.width()):
            px = image.pixelColor(x, y)
            if px.alpha() > 200:
                counts[px.rgb()] = counts.get(px.rgb(), 0) + 1
    assert counts, "icon rendered nothing"
    return QColor(max(counts, key=lambda k: counts[k]))


@pytest.mark.parametrize("name", [*icons.ICON_NAMES, *icons.BADGE_NAMES])
def test_every_icon_loads_and_is_legible_on_light_and_dark(qtbot: QtBot, name: str) -> None:
    for pal in (LIGHT, DARK):
        ic = icons.icon(name, pal)
        assert not ic.isNull() and not ic.pixmap(24, 24).isNull()
        bg = pal.color(QPalette.ColorRole.Window)
        assert contrast(ink(ic), bg) >= 2.5, f"{name} is hard to see on {bg.name()}"


def test_counts_match_the_mockup() -> None:
    assert len(icons.ICON_NAMES) == 18 and len(icons.BADGE_NAMES) == 5


def test_monochrome_icons_follow_the_palette_and_fixed_ones_do_not(qtbot: QtBot) -> None:
    assert ink(icons.icon("settings", LIGHT)) != ink(icons.icon("settings", DARK))
    assert ink(icons.icon("keep", LIGHT)) == ink(icons.icon("keep", DARK))


def test_disabled_state_is_dimmer(qtbot: QtBot) -> None:
    pal = palette("#f4f6f5", "#101a1f")
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#9aa3a0"))
    ic = icons.icon("settings", pal)
    normal = ic.pixmap(24, 24, QIcon.Mode.Normal).toImage()
    disabled = ic.pixmap(24, 24, QIcon.Mode.Disabled).toImage()
    assert normal != disabled


def test_app_icon_and_symbolic_exist(qtbot: QtBot) -> None:
    assert not icons.app_icon().isNull()
    assert "currentColor" not in icons.svg_text(icons.APP_ICON)
    assert icons.svg_text(icons.APP_ICON + "-symbolic")
