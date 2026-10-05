#!/usr/bin/env python
"""Render the hicolor icon tree under data/icons from the SVG sources in the package.

    data/icons/hicolor/scalable/apps/io.github.davidcampelo.Dedupe.svg
    data/icons/hicolor/symbolic/apps/io.github.davidcampelo.Dedupe-symbolic.svg
    data/icons/hicolor/{16,24,32,48,64,128,256}x{N}/apps/io.github.davidcampelo.Dedupe.png

Sizes of 32 px and below use the simplified artwork (solid ghost sheet, no folded corner), as in
the design mockup. Run it after changing an app icon; tests fail if the tree is out of date.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dedupe" / "gui" / "resources" / "icons"
OUT = ROOT / "data" / "icons" / "hicolor"
APP_ID = "io.github.davidcampelo.Dedupe"
SIZES = (16, 24, 32, 48, 64, 128, 256)
SIMPLIFIED_UP_TO = 32


def render(svg: Path, size: int, target: Path) -> None:
    renderer = QSvgRenderer(QByteArray(svg.read_bytes()))
    if not renderer.isValid():
        raise SystemExit(f"render_png_icons: {svg} is not a valid SVG")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(target)):
        raise SystemExit(f"render_png_icons: could not write {target}")


def main() -> None:
    QGuiApplication.instance() or QGuiApplication(sys.argv)
    for name in (APP_ID, f"{APP_ID}-small", f"{APP_ID}-symbolic"):
        if not (SRC / f"{name}.svg").is_file():
            raise SystemExit(f"render_png_icons: missing source icon {SRC / (name + '.svg')}")
    (OUT / "scalable" / "apps").mkdir(parents=True, exist_ok=True)
    (OUT / "symbolic" / "apps").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC / f"{APP_ID}.svg", OUT / "scalable" / "apps" / f"{APP_ID}.svg")
    shutil.copyfile(
        SRC / f"{APP_ID}-symbolic.svg", OUT / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg"
    )
    for size in SIZES:
        source = SRC / (f"{APP_ID}-small.svg" if size <= SIMPLIFIED_UP_TO else f"{APP_ID}.svg")
        render(source, size, OUT / f"{size}x{size}" / "apps" / f"{APP_ID}.png")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
