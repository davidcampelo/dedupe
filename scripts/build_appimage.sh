#!/usr/bin/env bash
# Build dist/Dedupe-<version>-x86_64.AppImage.
#
# The AppImage bundles a relocatable CPython (python-build-standalone), the app's own
# dependencies (PySide6-Essentials instead of the much larger PySide6 meta package) and the app.
# It needs the usual system Qt libraries (libxcb-cursor0, libEGL, libGL, fonts ...).
#
# Every missing input is a hard failure, and the *built artifact* is what gets verified: it is
# run (--version, a headless GUI self-test) and unpacked to check the desktop file and icon.
#
# Environment: PYTHON (interpreter that has `build`; default .venv or python3),
#              PBS_URL (override the python-build-standalone download).
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "build_appimage: $*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die "Linux only"
[[ "$(uname -m)" == x86_64 ]] || die "x86_64 only (found $(uname -m))"
for tool in curl tar file python3 desktop-file-validate; do
  command -v "$tool" >/dev/null || die "required tool not found: $tool"
done

PYTHON=${PYTHON:-}
if [[ -z "$PYTHON" ]]; then
  if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi
"$PYTHON" -c 'import build' 2>/dev/null || die "the 'build' package is missing for $PYTHON (pip install -e '.[dev]')"

APP_ID=io.github.davidcampelo.Dedupe
ICONS=data/icons/hicolor

# -- inputs: all must exist and be non-empty ------------------------------------------------------
required=(
  pyproject.toml
  "data/$APP_ID.desktop"
  "$ICONS/scalable/apps/$APP_ID.svg"
  "$ICONS/symbolic/apps/$APP_ID-symbolic.svg"
)
for size in 16 24 32 48 64 128 256; do required+=("$ICONS/${size}x${size}/apps/$APP_ID.png"); done
for f in "${required[@]}"; do [[ -s "$f" ]] || die "missing or empty input: $f (run scripts/render_png_icons.py?)"; done
desktop-file-validate "data/$APP_ID.desktop" || die "the desktop file is invalid"

version=$("$PYTHON" -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
[[ -n "$version" ]] || die "could not read the version from pyproject.toml"
out="dist/Dedupe-$version-x86_64.AppImage"
PWD_ROOT=$PWD

work=build/appimage
cache=build/cache
mkdir -p "$cache" dist

download() { # url, destination
  [[ -s "$2" ]] && return 0
  echo "downloading $1"
  curl -fL --retry 3 -o "$2.part" "$1" || die "download failed: $1"
  [[ -s "$2.part" ]] || die "empty download: $1"
  mv "$2.part" "$2"
}

# -- tools and the interpreter -----------------------------------------------------------------
tool="$cache/appimagetool-x86_64.AppImage"
download "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" "$tool"
chmod +x "$tool"
file "$tool" | grep -q ELF || die "appimagetool is not an executable (rm $tool and retry)"
# appimagetool can download its own runtime, but that fails behind some proxies; fetch it here.
runtime="$cache/runtime-x86_64"
download "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64" "$runtime"
file "$runtime" | grep -q ELF || die "the AppImage runtime is not an executable (rm $runtime and retry)"

pbs_url=${PBS_URL:-}
if [[ -z "$pbs_url" ]]; then
  pbs_url=$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest |
    "$PYTHON" -c '
import json, re, sys
for a in json.load(sys.stdin).get("assets", []):
    if re.fullmatch(r"cpython-3\.12\.\d+\+\d+-x86_64-unknown-linux-gnu-install_only_stripped\.tar\.gz", a["name"]):
        print(a["browser_download_url"]); break
') || die "could not query python-build-standalone releases (set PBS_URL)"
  [[ -n "$pbs_url" ]] || die "no CPython 3.12 build found in the latest python-build-standalone release (set PBS_URL)"
fi
pbs="$cache/$(basename "$pbs_url")"
download "$pbs_url" "$pbs"

# -- the AppDir --------------------------------------------------------------------------------
rm -rf "$work"
mkdir -p "$work/AppDir/usr"
tar -xzf "$pbs" -C "$work/AppDir/usr" || die "could not unpack $pbs"
py="$work/AppDir/usr/python/bin/python3"
[[ -x "$py" ]] || die "the bundled interpreter is missing after unpacking ($py)"

# Dependencies come from pyproject.toml, so this list cannot drift from the package metadata.
# PySide6 is swapped for PySide6-Essentials (QtCore/Gui/Widgets/Svg are all we use).
mapfile -t deps < <("$PYTHON" - <<'PY'
import re, tomllib
meta = tomllib.load(open("pyproject.toml", "rb"))["project"]
extras = meta.get("optional-dependencies", {})
# "heif" adds HEIC thumbnails; "similar" adds `dedupe scan --similar` and the Similar Images tab.
reqs = list(meta["dependencies"]) + list(extras.get("heif", [])) + list(extras.get("similar", []))
for r in reqs:
    print(re.sub(r"^PySide6(?![-\w])", "PySide6-Essentials", r))
PY
)
[[ ${#deps[@]} -ge 8 ]] || die "expected at least 8 dependencies from pyproject.toml, found ${#deps[@]}"
echo "dependencies: ${deps[*]}"
"$py" -m pip install --quiet --disable-pip-version-check --no-warn-script-location "${deps[@]}" \
  || die "installing dependencies failed"

rm -rf "$work/wheel"
"$PYTHON" -m build --wheel --outdir "$work/wheel" . >/dev/null || die "building the wheel failed"
wheels=("$work"/wheel/dedupe-*.whl)
[[ ${#wheels[@]} -eq 1 && -s "${wheels[0]}" ]] || die "expected exactly one wheel in $work/wheel"
"$py" -m pip install --quiet --disable-pip-version-check --no-deps "${wheels[0]}" \
  || die "installing the app wheel failed"

# Trim what we never use (QML, examples, headers, translations); the self-test below proves the
# result still starts.
site=$("$py" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
for junk in PySide6/Qt/qml PySide6/Qt/translations PySide6/examples PySide6/include \
            PySide6/typesystems PySide6/glue PySide6/scripts PySide6/Qt/metatypes; do
  rm -rf "${site:?}/$junk"
done
find "$work/AppDir" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

cat > "$work/AppDir/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONNOUSERSITE=1
exec "$HERE/usr/python/bin/python3" -m dedupe "$@"
RUN
chmod +x "$work/AppDir/AppRun"

cp "data/$APP_ID.desktop" "$work/AppDir/$APP_ID.desktop"
cp "$ICONS/256x256/apps/$APP_ID.png" "$work/AppDir/$APP_ID.png"
ln -sf "$APP_ID.png" "$work/AppDir/.DirIcon"
mkdir -p "$work/AppDir/usr/share/applications" "$work/AppDir/usr/share/icons"
cp "data/$APP_ID.desktop" "$work/AppDir/usr/share/applications/"
cp -r data/icons/hicolor "$work/AppDir/usr/share/icons/"

# -- build ----------------------------------------------------------------------------------------
rm -f "$out"
ARCH=x86_64 "$tool" --appimage-extract-and-run --runtime-file "$runtime" "$work/AppDir" "$out" >"$work/appimagetool.log" 2>&1 \
  || { tail -n 30 "$work/appimagetool.log" >&2; die "appimagetool failed (log: $work/appimagetool.log)"; }
[[ -s "$out" ]] || die "no AppImage was produced"

# -- verify the artifact itself, not the source tree ------------------------------------------------
size=$(stat -c %s "$out")
min=$((40 * 1024 * 1024)); max=$((900 * 1024 * 1024))
(( size >= min )) || die "AppImage is only $size bytes (< $min): something is missing from it"
(( size <= max )) || die "AppImage is $size bytes (> $max): something unexpected got bundled"
file "$out" | grep -q ELF || die "$out is not an ELF executable"

export APPIMAGE_EXTRACT_AND_RUN=1
reported=$("$out" --version) || die "'$out --version' failed"
[[ "$reported" == "dedupe $version" ]] || die "'--version' printed '$reported', expected 'dedupe $version'"
QT_QPA_PLATFORM=offscreen "$out" --self-test | grep -q "self-test: ok" \
  || die "the headless GUI self-test failed from inside the AppImage"

check=$(mktemp -d)
trap 'rm -rf "$check"' EXIT
# The similar-images extra must work from the artifact: two near-identical pictures (the second is
# a smaller, re-compressed copy) must come back as one similar group.
mkdir "$check/pics"
"$py" - "$check/pics" <<'PY' || die "could not generate test images with the bundled interpreter"
import sys
import numpy as np
from PIL import Image
rng = np.random.default_rng(1)
img = Image.fromarray(rng.integers(0, 256, (9, 12, 3), dtype=np.uint8)).resize((320, 240), Image.BICUBIC)
img.save(f"{sys.argv[1]}/a.png")
img.resize((200, 150)).save(f"{sys.argv[1]}/b.jpg", quality=60)
PY
found=$("$out" scan "$check/pics" --similar --no-cache --json) || die "'scan --similar' failed inside the AppImage"
grep -q '"similar_groups": \[$' <<<"$found" \
  || die "'scan --similar' did not report similar_groups from inside the AppImage"
grep -q 'b.jpg' <<<"$found" \
  || die "'scan --similar' did not find the near-identical pair from inside the AppImage"
(cd "$check" && "$PWD_ROOT/$out" --appimage-extract >/dev/null) || die "could not unpack the AppImage"
[[ -s "$check/squashfs-root/$APP_ID.desktop" ]] || die "the AppImage has no desktop file"
[[ -s "$check/squashfs-root/$APP_ID.png" ]] || die "the AppImage has no icon"
[[ -s "$check/squashfs-root/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg" ]] || die "the AppImage has no scalable icon"

echo "built $out ($((size / 1024 / 1024)) MiB); --version and the GUI self-test passed from the AppImage"
