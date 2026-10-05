"""GUI entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMainWindow

APP_ID = "io.github.davidcampelo.Dedupe"


def main() -> int:
    app = QApplication(sys.argv)
    app.setDesktopFileName(APP_ID)
    window = QMainWindow()
    window.setWindowTitle("Dedupe")
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
