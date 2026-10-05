"""GUI entry point."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from dedupe.core.settings import SettingsError, load_settings
from dedupe.gui import icons
from dedupe.gui.gcutil import tune_runtime
from dedupe.gui.main_window import MainWindow
from dedupe.gui.stall import StallMonitor, log_stall

APP_ID = "io.github.davidcampelo.Dedupe"


def main() -> int:
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
