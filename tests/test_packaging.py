"""Packaging checks: the icon tree is current, the desktop file is valid, the entry points work
and the built wheel contains everything the installed app needs."""

from __future__ import annotations

import configparser
import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from PySide6.QtGui import QImage
from pytestqt.qtbot import QtBot

import dedupe

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "io.github.davidcampelo.Dedupe"
HICOLOR = ROOT / "data" / "icons" / "hicolor"
RESOURCES = ROOT / "dedupe" / "gui" / "resources" / "icons"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
SIZES = (16, 24, 32, 48, 64, 128, 256)


def load_script(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_matches_pyproject() -> None:
    assert dedupe.__version__ == PYPROJECT["project"]["version"]


def test_scalable_and_symbolic_icons_are_copies_of_the_package_icons() -> None:
    assert (HICOLOR / "scalable/apps" / f"{APP_ID}.svg").read_bytes() == (
        RESOURCES / f"{APP_ID}.svg"
    ).read_bytes()
    symbolic = HICOLOR / "symbolic/apps" / f"{APP_ID}-symbolic.svg"
    assert symbolic.read_bytes() == (RESOURCES / f"{APP_ID}-symbolic.svg").read_bytes()


@pytest.mark.parametrize("size", SIZES)
def test_png_icons_exist_with_the_right_dimensions(size: int) -> None:
    image = QImage(str(HICOLOR / f"{size}x{size}/apps/{APP_ID}.png"))
    assert (image.width(), image.height()) == (size, size)
    assert image.hasAlphaChannel()


ANTIALIAS_TOLERANCE = 24  # per channel; Qt builds differ slightly in edge antialiasing


def max_channel_diff(a: QImage, b: QImage) -> int:
    if a.size() != b.size():
        return 255
    a = a.convertToFormat(QImage.Format.Format_ARGB32)
    b = b.convertToFormat(QImage.Format.Format_ARGB32)
    worst = 0
    for y in range(a.height()):
        for x in range(a.width()):
            p, q = a.pixel(x, y), b.pixel(x, y)
            worst = max(worst, *(abs(((p >> s) & 255) - ((q >> s) & 255)) for s in (0, 8, 16, 24)))
    return worst


def test_png_icons_are_up_to_date_with_the_svg_sources(qtbot: QtBot, tmp_path: Path) -> None:
    render = load_script("render_png_icons")
    for size in SIZES:
        source = RESOURCES / (
            f"{APP_ID}-small.svg" if size <= render.SIMPLIFIED_UP_TO else f"{APP_ID}.svg"
        )
        fresh = tmp_path / f"{size}.png"
        render.render(source, size, fresh)
        committed = QImage(str(HICOLOR / f"{size}x{size}/apps/{APP_ID}.png"))
        assert max_channel_diff(QImage(str(fresh)), committed) <= ANTIALIAS_TOLERANCE, (
            f"{size}px icon is stale: run scripts/render_png_icons.py"
        )


def desktop_entry() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(ROOT / "data" / f"{APP_ID}.desktop", encoding="utf-8")
    return parser["Desktop Entry"]


def test_desktop_file_fields() -> None:
    entry = desktop_entry()
    assert entry["Type"] == "Application" and entry["Name"] == "Dedupe"
    assert entry["Icon"] == APP_ID
    assert entry["Exec"].split()[0] in PYPROJECT["project"]["gui-scripts"]
    assert entry["Terminal"] == "false"
    assert "Utility" in entry["Categories"].split(";")


@pytest.mark.skipif(
    shutil.which("desktop-file-validate") is None, reason="desktop-file-utils not installed"
)
def test_desktop_file_validates() -> None:
    result = subprocess.run(
        ["desktop-file-validate", str(ROOT / "data" / f"{APP_ID}.desktop")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_entry_points_are_declared() -> None:
    assert PYPROJECT["project"]["scripts"]["dedupe"] == "dedupe.cli:main"
    assert PYPROJECT["project"]["gui-scripts"]["dedupe-gui"] == "dedupe.gui.app:main"


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    return subprocess.run(
        [sys.executable, "-m", "dedupe", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_python_m_dedupe_version_and_cli() -> None:
    result = run_module("--version")
    assert result.returncode == 0 and result.stdout.strip() == f"dedupe {dedupe.__version__}"
    assert "usage" in run_module("--help").stdout.lower()


def test_python_m_dedupe_self_test_starts_the_gui_offscreen() -> None:
    result = run_module("--self-test")
    assert result.returncode == 0, result.stderr
    assert "self-test: ok" in result.stdout


def test_main_dispatches_between_cli_and_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    from dedupe import __main__ as entry

    called: list[str] = []
    monkeypatch.setattr("dedupe.cli.main", lambda argv: called.append(f"cli {argv}") or 0)
    monkeypatch.setattr("dedupe.gui.app.main", lambda: called.append("gui") or 0)
    assert entry.main(["scan", "/x"]) == 0
    assert entry.main(["--version"]) == 0
    assert entry.main([]) == 0
    assert called == ["cli ['scan', '/x']", "cli ['--version']", "gui"]


@pytest.mark.slow
def test_built_wheel_contains_the_app_icons_desktop_file_and_entry_points(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    (wheel,) = tmp_path.glob("dedupe-*.whl")
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        entry_points = z.read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
    data = f"dedupe-{dedupe.__version__}.data/data/share"
    expected = {
        "dedupe/__init__.py",
        "dedupe/__main__.py",
        "dedupe/core/actions.py",
        "dedupe/gui/main_window.py",
        f"dedupe/gui/resources/icons/{APP_ID}.svg",
        f"dedupe/gui/resources/icons/{APP_ID}-symbolic.svg",
        f"{data}/applications/{APP_ID}.desktop",
        f"{data}/icons/hicolor/scalable/apps/{APP_ID}.svg",
        f"{data}/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg",
        *(f"{data}/icons/hicolor/{s}x{s}/apps/{APP_ID}.png" for s in SIZES),
    }
    assert not expected - names, f"missing from the wheel: {sorted(expected - names)}"
    ui_icons = [
        n for n in names if n.startswith("dedupe/gui/resources/icons/") and n.endswith(".svg")
    ]
    assert len(ui_icons) >= 18 + 5 + 3  # UI icons + badges + app icon variants
    assert not any(n.startswith("tests/") for n in names)
    assert (
        "dedupe = dedupe.cli:main" in entry_points
        and "dedupe-gui = dedupe.gui.app:main" in entry_points
    )
    assert "[gui_scripts]" in entry_points


def test_dedupe_gui_answers_help_and_version_without_a_display() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env["QT_QPA_PLATFORM"] = "xcb"  # would abort if Qt were started
    for flag, expected in (
        ("--version", f"dedupe {dedupe.__version__}"),
        ("--help", "usage: dedupe-gui"),
    ):
        result = subprocess.run(
            [sys.executable, "-c", "from dedupe.gui.app import main; main()", flag],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0 and expected in result.stdout, result.stderr


def test_the_similar_extra_is_declared_installed_for_dev_and_bundled_in_the_appimage() -> None:
    extras = PYPROJECT["project"]["optional-dependencies"]
    assert any(r.startswith("imagehash") and "<5" in r for r in extras["similar"])
    assert any(r.startswith("numpy>=2") for r in extras["similar"])  # np.bitwise_count
    assert all(r in extras["dev"] for r in extras["similar"])  # the gate must run the feature
    script = (ROOT / "scripts" / "build_appimage.sh").read_text()
    assert 'extras.get("similar"' in script and 'scan "$check/pics" --similar' in script
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert ".[dev,similar]" in ci
