#!/usr/bin/env python
"""Render a contact sheet of every icon on light and dark palettes, for human review.

Usage: python scripts/render_icons.py [out.png]   (default: icons-contact-sheet.png)
"""

from __future__ import annotations

import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPalette
from PySide6.QtWidgets import QApplication

from dedupe.gui import icons

CELL_W, CELL_H, COLS = 150, 110, 6


def palette(dark: bool) -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor("#1c2024" if dark else "#f4f6f5"))
    p.setColor(QPalette.ColorRole.WindowText, QColor("#e6ebe9" if dark else "#101a1f"))
    return p


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    _ = app, QGuiApplication
    out = sys.argv[1] if len(sys.argv) > 1 else "icons-contact-sheet.png"
    names = (*icons.ICON_NAMES, *icons.BADGE_NAMES, icons.APP_ICON)
    rows = -(-len(names) // COLS)
    image = QImage(CELL_W * COLS * 2, CELL_H * rows, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    for half, dark in enumerate((False, True)):
        pal = palette(dark)
        painter.fillRect(half * CELL_W * COLS, 0, CELL_W * COLS, CELL_H * rows, pal.window())
        painter.setPen(pal.color(QPalette.ColorRole.WindowText))
        painter.setFont(QFont("sans", 8))
        for i, name in enumerate(names):
            x = half * CELL_W * COLS + (i % COLS) * CELL_W
            y = (i // COLS) * CELL_H
            ic = icons.app_icon() if name == icons.APP_ICON else icons.icon(name, pal)
            pm = ic.pixmap(48, 48)
            painter.drawPixmap(x + (CELL_W - 48) // 2, y + 12, pm)
            painter.drawPixmap(x + CELL_W // 2 - 40, y + 66, ic.pixmap(16, 16))
            painter.drawPixmap(x + CELL_W // 2 + 24, y + 66, ic.pixmap(24, 24))
            painter.drawText(QRect(x, y + 90, CELL_W, 16), Qt.AlignmentFlag.AlignHCenter, name)
    painter.end()
    image.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
