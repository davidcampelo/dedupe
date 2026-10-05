"""GUI entry point."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from dedupe import __version__
from dedupe.core.settings import SettingsError, load_settings
from dedupe.gui import icons
from dedupe.gui.gcutil import tune_runtime
from dedupe.gui.main_window import MainWindow
from dedupe.gui.stall import StallMonitor, log_stall

APP_ID = "io.github.davidcampelo.Dedupe"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dedupe-gui",
        description="Dedupe: find duplicate files and clean hidden/temp files, safely.",
        epilog="Qt options such as -platform offscreen are passed through.",
    )
    parser.add_argument("--version", action="version", version=f"dedupe {__version__}")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main() -> int:
    parse_args(sys.argv[1:])  # handles --help/--version before Qt (which needs a display) starts
    tune_runtime()
    app = QApplication(sys.argv)
    app.setApplicationName("Dedupe")
    app.setOrganizationName("davidcampelo")
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(icons.app_icon())

    monitor: StallMonitor | None = None
    if os.environ.get("DEDUPE_STALL_LOG") == "1":
        monitor = StallMonitor(on_stall=log_stall, sample_stack=True)
        monitor.start()

    try:
        loaded = load_settings()
        settings, warnings = loaded.settings, loaded.warnings
    except SettingsError as e:
        settings, warnings = None, (f"settings ignored: {e}",)
    window = MainWindow(settings, QSettings())
    for warning in warnings:
        window.statusBar().showMessage(warning, 10000)
    window.show()
    code = app.exec()
    if monitor is not None:
        monitor.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
