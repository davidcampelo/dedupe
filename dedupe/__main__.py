"""``python -m dedupe``: the command-line tool, or the GUI when no CLI command is given.

This is the single entry point the AppImage uses (``AppRun`` calls it), so one executable gives
both ``Dedupe.AppImage scan PATH`` and a plain double-click that opens the window."""

from __future__ import annotations

import sys

CLI_WORDS = {"scan", "hidden", "cache", "--version", "-h", "--help"}


def self_test() -> int:
    """Create the main window offscreen and exit. Proves, from an installed or packaged copy,
    that PySide6, the Qt platform plugin, the bundled icons and the GUI modules all load."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from dedupe.gui import icons
    from dedupe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    window.show()
    missing = [n for n in (*icons.ICON_NAMES, *icons.BADGE_NAMES) if icons.icon(n).isNull()]
    if missing or icons.app_icon().isNull():
        print(f"dedupe self-test: missing icons: {missing}", file=sys.stderr)
        return 1
    window.close()
    del app
    print("dedupe self-test: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--self-test"]:
        return self_test()
    if args and args[0] in CLI_WORDS:
        from dedupe.cli import main as cli_main

        return cli_main(args)
    from dedupe.gui.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
