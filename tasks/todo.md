# Dedupe: Task List

Plan, decisions and rationale: [plan.md](plan.md). Spec: [docs/duplicate-finder-prompt.md](../docs/duplicate-finder-prompt.md).

**Every task's gate:** `scripts/check.sh --fast` passes (pre-commit and the Claude `Stop` hook enforce it). Each task ends with one commit and its box ticked here.
**Every checkpoint's gate:** `scripts/check.sh` (full: slow tests + ≥ 90 % core coverage), the manual checklist, and human review before the next phase starts.
**Every GUI task's gate:** its tests run under the `ui_watchdog` fixture (no GUI-thread gap over 100 ms).

---

## Phase 0: Gates

### Task 0: Quality gates and hooks ✅

**Description:** A single gate script, plus git hooks, Claude Code hooks and CI that all call it.

**Acceptance criteria:**
- [x] `scripts/check.sh [--fast]` runs ruff, ruff format, mypy and pytest (the full mode adds slow tests and core coverage); it no-ops until `dedupe/` exists and fails if a tool is missing
- [x] `.githooks/pre-commit` (fast) and `pre-push` (full) are enabled via `core.hooksPath` (also set in `post-create.sh`)
- [x] `.claude/settings.json`: `PostToolUse` ruff on edited `.py` files, `PreToolUse` guard against bypassing hooks, `Stop` runs the fast gate when Python files changed
- [x] `.github/workflows/ci.yml` runs the full gate on push/PR

**Verification:** each hook was pipe-tested (fixable lint → auto-fixed and exit 0; F821 → exit 2 with the message; bypass commands → blocked; `.githooks` hooksPath → allowed). The guard was also seen blocking a live Bash call.

**Files:** `scripts/check.sh`, `.githooks/*`, `.claude/settings.json`, `.claude/hooks/*.sh`, `.github/workflows/ci.yml`, `.devcontainer/post-create.sh`

---

## Phase 1: Headless engine slice

### Task 1: Project scaffold, models, tooling, test safety nets

**Description:** Create `pyproject.toml` (hatchling, `requires-python = ">=3.12"`). Runtime deps: PySide6, blake3, xxhash, Pillow, send2trash, tomli-w; extras: `heif` (pillow-heif) and `dev` (pytest, pytest-qt, pytest-cov, ruff, mypy). Add the `dedupe`/`dedupe-gui` entry points. Tool config: ruff `target-version = "py312"`; mypy with `files = ["dedupe"]`, strict on `dedupe.core`, typed defs required in `dedupe.gui`; pytest with `--strict-markers` and a registered `slow` marker. Add the package skeleton from spec §3 and `core/models.py` (frozen dataclasses `FileEntry`, `DuplicateGroup`, `Recommendation`, `ScanOptions`, `SkippedEntry`, `ScanResult`, `Progress`, plus `CancelToken`). Add two safety-net tests: the **isolation fixture** (autouse; `HOME` and `XDG_*` point at `tmp_path`) and the **deletion audit** (`ast` scan; deletion calls are allowed only in `core/actions.py` plus an allowlist with reasons).

**Acceptance criteria:**
- [ ] `pip install -e ".[dev]"` works; `dedupe --help` runs; `dedupe-gui` opens an empty window
- [ ] `ScanOptions` defaults match spec §4; `scripts/check.sh` (full) passes on the skeleton
- [ ] The deletion-audit test fails when a stray `os.remove` is added to any other module (checked by hand once)

**Dependencies:** T0

**Files:** `pyproject.toml`, `dedupe/__init__.py`, `dedupe/core/models.py`, `dedupe/cli.py`, `dedupe/gui/app.py`, `tests/conftest.py`, `tests/test_deletion_audit.py`

**Scope:** M

### Task 2: Scanner ✅

**Description:** `core/scanner.py`: an iterative `os.scandir` walk using an explicit stack that yields `FileEntry` (path, size, mtime_ns, inode, device, mode). Symlink following is opt-in, with `(dev, inode)` loop protection. It stays on one filesystem by default and applies glob and folder exclusions, min size and the hidden-files option. Unreadable or vanished entries become `SkippedEntry(path, reason)`. It reports progress and checks the `CancelToken` on every entry.

**Acceptance criteria:**
- [x] Symlinks are skipped by default; with following enabled, a symlink loop terminates
- [x] A `chmod 000` dir/file lands in `skipped` with a reason and the scan continues (skipped when running as root)
- [x] Exclusions and min size are respected; cancelling mid-walk returns within 100 ms

**Dependencies:** T1

**Files:** `dedupe/core/scanner.py`, `tests/core/test_scanner.py`, `tests/conftest.py` (tree-builder fixture)

**Scope:** S

### Task 3: Hasher and grouper

**Description:** `core/hasher.py`: `partial_hash` (xxh3_128 of size + head 64 KiB + tail 64 KiB) and `full_hash` (BLAKE3, 1 MiB chunks, checking cancel per chunk), run in a thread pool that reports progress in bytes. A read error moves the file to skipped. `core/grouper.py` is a chain of `GroupingStage`s: size → hardlink collapse → partial → full → optional byte compare. Empty files are returned as their own category.

**Acceptance criteria:**
- [ ] Identical content with different names/dates is grouped; same size with different content is not
- [ ] Files that differ only in the middle (> 128 KiB, same head and tail) are separated by the full hash
- [ ] Hard links are never deletable duplicates; empty files are not a group; paranoid mode agrees with the hash

**Dependencies:** T2

**Files:** `dedupe/core/hasher.py`, `dedupe/core/grouper.py`, two test files

**Scope:** M

### Task 4: Pipeline and `dedupe scan` CLI

**Description:** `core/pipeline.py` `run_scan(...) -> ScanResult`. `cli.py` adds `dedupe scan PATH [--min-size N] [--exclude GLOB]... [--json]`, with a table by default and a stable JSON schema with `--json`. Ctrl-C triggers the cancel token.

**Acceptance criteria:**
- [ ] The JSON lists groups (hash, size, paths, reclaimable), empty files and skipped entries
- [ ] A bad path gives a non-zero exit code; Ctrl-C exits cleanly
- [ ] Running on `/files` completes with plausible results

**Dependencies:** T3

**Files:** `dedupe/core/pipeline.py`, `dedupe/cli.py`, `tests/test_cli.py`

**Scope:** S

## Checkpoint A: Headless slice
- [ ] `scripts/check.sh` (full) passes; CI is green
- [ ] `dedupe scan` gives correct results on fixtures and on `/files`
- [ ] Human review before Phase 2

---

## Phase 2: Complete the core

### Task 5: Hash cache

**Description:** `core/cache.py`: SQLite at `$XDG_CACHE_HOME/dedupe/hashes.db` (WAL), keyed on `(path, size, mtime_ns, inode, device)`. A single writer thread does batched commits. Provides `clear()`, the `--no-cache` flag and `dedupe cache clear`.

**Acceptance criteria:**
- [ ] A second scan reads zero bytes for unchanged files (hash-call counter)
- [ ] Changing the content, mtime or inode invalidates only that entry
- [ ] A corrupt or locked DB degrades to no-cache with a warning and the scan still succeeds

**Dependencies:** T3

**Files:** `dedupe/core/cache.py`, `dedupe/core/hasher.py`, `dedupe/cli.py`, `tests/core/test_cache.py`

**Scope:** S

### Task 6: Recommender

**Description:** `core/recommender.py` applies the spec §5 rules in order and produces exactly one Keep plus a reason per group. Protected files are never marked Delete. Bulk helpers: keep newest, keep in folder X, select all suggested. The output appears in the CLI.

**Acceptance criteria:**
- [ ] A parametrized test per rule proves that rule decides when the earlier rules tie
- [ ] Property test: exactly one Keep per group, and no protected file is ever marked Delete
- [ ] An all-protected group marks every file Keep

**Dependencies:** T1, T4

**Files:** `dedupe/core/recommender.py`, `dedupe/core/pipeline.py`, `dedupe/cli.py`, test file

**Scope:** S

### Task 7: Hidden/temp detector and `dedupe hidden` CLI

**Description:** `core/hidden.py` classifies `.x`, `~x`, `~$x`, `x~` and the optional temp patterns. Each result carries a category, a type, its size (recursive for dirs) and the `protected`, `home_toplevel_dot` and `preselect` flags (`*.swp` is preselected only when no `/proc/*/fd` holds it). Adds `dedupe hidden PATH [--json]`.

**Acceptance criteria:**
- [ ] `.x`, `~x`, `x~` and `~$x.docx` are classified correctly; a dot folder is reported once
- [ ] Protected dotfiles are flagged and never preselected
- [ ] A swap file held open by a test process is not preselected

**Dependencies:** T2

**Files:** `dedupe/core/hidden.py`, `dedupe/cli.py`, test file

**Scope:** S

### Task 8: Settings

**Description:** `core/settings.py`: XDG path helpers and a `Settings` dataclass covering every field in the spec §7 settings dialog. Loads and saves `settings.toml` (`tomllib`/`tomli-w`) with validation: unknown keys warn, wrong types fail, no `bool`-as-`int`, and falsy values are preserved. Converts to `ScanOptions`.

**Acceptance criteria:**
- [ ] Save → load round-trips to an equal object; a missing file gives the defaults
- [ ] A misspelled key warns; a mistyped value fails with an error naming the key
- [ ] XDG env vars are honoured with the standard fallbacks

**Dependencies:** T1

**Files:** `dedupe/core/settings.py`, test file

**Scope:** S

### Task 9: Actions, part 1 (plan, guards, dry run, trash, log)

**Description:** `core/actions.py`: `plan_actions(groups, selection, mode)` refuses any selection that would remove the last copy of a group or touch a protected path. `execute(plan, dry_run, progress, cancel)` re-verifies each file (exists, same size/mtime_ns/inode) immediately before acting and skips changed files. It moves files to Trash with send2trash, appends JSON lines to `$XDG_DATA_HOME/dedupe/actions.log`, reports progress per file and can be cancelled between files.

**Acceptance criteria:**
- [ ] Selecting every copy of a group is refused before any mutation happens
- [ ] A file modified after the scan is skipped as "changed since scan"
- [ ] A dry run leaves the tree byte-identical but produces the same plan and summary; a Trash failure is reported, not retried as a permanent delete

**Verification:** plus a mutation check: disabling each guard in turn makes at least one test fail

**Dependencies:** T3, T6

**Files:** `dedupe/core/actions.py`, `tests/core/test_actions.py`

**Scope:** M

### Task 10: Actions, part 2 (permanent delete and hardlink replacement)

**Description:** Adds the `PERMANENT` mode (`os.unlink`) and the `HARDLINK` mode (same-device check, `os.link(keep, tmp)` in the duplicate's directory, then `os.replace(tmp, dup)`, with tmp cleaned up on failure). `plan_actions` reports whether hardlinking is possible.

**Acceptance criteria:**
- [ ] After hardlinking, every path exists with the same content and they share one inode
- [ ] A cross-device selection makes hardlink unavailable; a failure midway leaves no tmp files and no missing paths
- [ ] Permanent delete removes only the planned files and logs each one

**Dependencies:** T9

**Files:** `dedupe/core/actions.py`, test file

**Scope:** S

## Checkpoint B: Core complete
- [ ] `scripts/check.sh` (full) passes; core coverage ≥ 90 %; CI is green
- [ ] Every safety guard is mutation-checked
- [ ] `scripts/bench.py` (100k synthetic files): cold and warm-cache times and cancel latency recorded in plan.md
- [ ] Human review before the GUI starts

---

## Phase 3: GUI vertical slice

### Task 11: Icon set

**Description:** Redraw the icons from `docs/UI icon set@2x.png` as SVGs (24 px grid, 1.75 stroke, round caps, `currentColor`): 18 icons plus 5 status badges. Draw the "Twin Sheets" app icon as `io.github.davidcampelo.Dedupe.svg` and `-symbolic.svg`. `gui/icons.py` loads them via `importlib.resources` and recolors from `QPalette`; keep/delete/protected/warning use fixed semantic colors.

**Acceptance criteria:**
- [ ] Every mockup icon exists and loads to a non-null `QIcon`
- [ ] Icons are legible on light and dark palettes
- [ ] A contact sheet (`scripts/render_icons.py`) next to the mockup has been approved by the human

**Dependencies:** T1

**Files:** `dedupe/gui/resources/icons/*.svg`, `dedupe/gui/icons.py`, `scripts/render_icons.py`, test file

**Scope:** M

### Task 12: Job runner, responsiveness gates, main window shell

**Description:** `gui/workers.py`: a generic `Job` (`QRunnable` + `CancelToken` + queued `progress/finished/failed` signals, progress throttled to ≤ 20 Hz) and `ScanJob`. Test infrastructure: the `ui_watchdog` fixture in `tests/gui/conftest.py`, the static `tests/test_gui_thread_rules.py`, and the `DEDUPE_STALL_LOG=1` runtime stall logger. `gui/main_window.py` has the folder button, drag-and-drop, recent folders, Scan/Cancel, a progress bar with stage text, placeholder tabs and the status bar. Closing the window cancels jobs and waits for them (bounded).

**Acceptance criteria:**
- [ ] pytest-qt smoke test: scanning a fixture folder reports the expected group count, **under `ui_watchdog`**
- [ ] A slow scan (an injected sleep per file) can be cancelled within 1 s, and the watchdog shows no gap over 100 ms
- [ ] The GUI-thread rules test fails when an `os.stat` is added to `main_window.py` (checked by hand once)

**Dependencies:** T4, T11

**Files:** `dedupe/gui/workers.py`, `dedupe/gui/main_window.py`, `dedupe/gui/app.py`, `tests/gui/conftest.py`, `tests/gui/test_main_window.py`, `tests/test_gui_thread_rules.py`

**Scope:** M

### Task 13: Duplicates tree

**Description:** `gui/duplicates_view.py`: a custom `QAbstractItemModel`. Group rows show the hash prefix, size, count and reclaimable bytes; file rows show a checkbox, path, size, mtime, a badge and the reason. Results are inserted in **batches** (≤ 16 ms per batch, via `QTimer.singleShot(0)`). Space toggles the selection, and user overrides are kept separate from the recommendation.

**Acceptance criteria:**
- [ ] Keep rows start unchecked and Delete rows checked; Space and the mouse toggle; the selected size updates
- [ ] Loading 100k files / 30k groups passes `ui_watchdog` (synthetic `ScanResult`, marked `slow`)
- [ ] `QAbstractItemModelTester` reports no errors

**Dependencies:** T12

**Files:** `dedupe/gui/duplicates_view.py`, `dedupe/gui/main_window.py`, test file

**Scope:** M

### Task 14: Delete confirmation flow

**Description:** `gui/delete_dialog.py`: the Delete key or button opens a dialog with the count, total size, the first N paths and the mode: Trash (default), Permanent (enabled only once the **"I understand these files cannot be recovered" checkbox** is ticked) or Hardlink (only when the plan allows it), plus a Dry run toggle. Planning and execution run in an **`ActionJob`** with per-file progress and cancel. Planner refusals appear as a blocking message. A summary dialog follows, and the model drops the deleted rows.

**Acceptance criteria:**
- [ ] Rejecting the dialog leaves every file intact; nothing reaches `execute` without acceptance
- [ ] In Permanent mode, OK stays disabled until the checkbox is ticked, and switching modes unticks it
- [ ] Deleting 5k fixture files (Trash, with a stubbed backend) passes `ui_watchdog`; a dry run leaves the files on disk

**Dependencies:** T10, T13

**Files:** `dedupe/gui/delete_dialog.py`, `dedupe/gui/workers.py`, `dedupe/gui/main_window.py`, `dedupe/gui/duplicates_view.py`, test file

**Scope:** M

## Checkpoint C: GUI slice
- [ ] `scripts/check.sh` (full) passes; CI is green
- [ ] Manual check against `/files`: scan → review → Trash → `actions.log` written, with `DEDUPE_STALL_LOG=1` reporting no stalls
- [ ] 100k-file synthetic run: scan, load and dry-run delete all pass the watchdog; cancel < 1 s
- [ ] Human review

---

## Phase 4: GUI completeness

### Task 15: Duplicates tab extras

**Description:** Sorting (reclaimable/size/count) and filters (file type, path text) are handled in the model, not via a proxy re-sort over 100k rows, and text input is debounced by 200 ms. Context menu: open file/folder (`QDesktopServices`), copy path, mark Keep, mark folder Protected (persisted, then the recommendations rerun). Details panel. Bulk-rule menu.

**Acceptance criteria:**
- [ ] Sorting and filtering keep group children together; filtering the 100k model passes `ui_watchdog`
- [ ] Marking a folder Protected updates every affected group immediately
- [ ] Every bulk rule leaves exactly one Keep per group

**Dependencies:** T13, T8

**Files:** `dedupe/gui/duplicates_view.py`, `dedupe/gui/details_panel.py`, `dedupe/gui/file_types.py`, test file

**Scope:** M

### Task 16: Hidden & Temp tab and Skipped/Errors tab

**Description:** The hidden scan runs in a `HiddenScanJob`. The table preselects per `preselect` and shows protected rows unchecked. Selecting protected items or anything inside a top-level home dot folder needs an extra confirmation. Shows the reclaimable total and reuses the T14 dialog and `ActionJob`. The Skipped tab lists path and reason.

**Acceptance criteria:**
- [ ] `~$x.docx` and `x~` are preselected; `.bashrc` is unchecked; ticking `~/.ssh/...` asks for confirmation
- [ ] The reclaimable total equals the sum of the selected sizes
- [ ] A hidden scan of a 50k-entry tree passes `ui_watchdog`

**Dependencies:** T7, T14

**Files:** `dedupe/gui/hidden_files_view.py`, `dedupe/gui/skipped_view.py`, `dedupe/gui/main_window.py`, test file

**Scope:** M

### Task 17: Thumbnails and image comparison

**Description:** `gui/thumbnails.py`: `ThumbnailJob`s on a dedicated bounded `QThreadPool`. They decode with Pillow (applying EXIF orientation, and HEIC when available). Thumbnails are cached in a memory LRU and on disk keyed by hash; the freedesktop cache is reused when valid. Jobs for groups that are no longer visible are cancelled. `gui/image_compare.py` shows the images side by side, each with resolution, size, mtime, EXIF date and a Keep/Delete toggle bound to the model. Double-clicking opens a preview.

**Acceptance criteria:**
- [ ] Placeholders appear at once; selecting 20 large-image groups quickly passes `ui_watchdog`
- [ ] A corrupt image shows an error placeholder and doesn't crash
- [ ] Keep/Delete stays in sync between the compare panel and the tree

**Dependencies:** T13

**Files:** `dedupe/gui/thumbnails.py`, `dedupe/gui/image_compare.py`, `dedupe/gui/preview_window.py`, tests

**Scope:** M

### Task 18: Thumbnail grid mode

**Description:** A toggle switches to a `QListView` in icon mode showing one tile per image group, with thumbnails requested lazily for visible tiles only. Clicking a tile selects the group.

**Acceptance criteria:**
- [ ] Scrolling 1k image groups passes `ui_watchdog`
- [ ] Switching modes preserves the selection

**Dependencies:** T17

**Files:** `dedupe/gui/image_grid.py`, `dedupe/gui/duplicates_view.py`, test file

**Scope:** S

### Task 19: Settings dialog, persisted state, theme

**Description:** `gui/settings_dialog.py` covers every spec §7 setting (backed by T8; clear cache runs as a Job). `QSettings` (org/app derived from `io.github.davidcampelo.Dedupe`) persists the geometry, splitters, last folder and recent folders. The theme follows `QStyleHints.colorScheme`. Keyboard navigation is audited.

**Acceptance criteria:**
- [ ] Changed settings save to `settings.toml` and apply to the next scan
- [ ] A restart restores the window size, splitters and last folder
- [ ] A system light/dark switch recolors the icons without a restart

**Dependencies:** T8, T12

**Files:** `dedupe/gui/settings_dialog.py`, `dedupe/gui/main_window.py`, `dedupe/gui/icons.py`, test file

**Scope:** M

## Checkpoint D: Feature-complete
- [ ] `scripts/check.sh` (full) passes; CI is green
- [ ] Every spec §7 feature checked by hand; keyboard-only walkthrough (arrows, Space, Delete)
- [ ] 100k-file watchdog run with every tab populated; `DEDUPE_STALL_LOG=1` is clean on `/files`
- [ ] Human review

---

## Phase 5: Ship

### Task 20: Packaging

**Description:** `data/io.github.davidcampelo.Dedupe.desktop`, plus hicolor icons (scalable, symbolic, and PNGs at 16–256 rendered from the SVG) shipped as package data. `scripts/build_appimage.sh` fails loudly on any missing input and checks the **built artifact** (`--version` run from the AppImage). The full gate in `check.sh` gains `desktop-file-validate`.

**Acceptance criteria:**
- [ ] `pipx install .` in a clean venv provides working `dedupe` and `dedupe-gui` commands
- [ ] `desktop-file-validate` passes as part of `scripts/check.sh`
- [ ] The AppImage builds in the devcontainer and `--version` runs from it

**Dependencies:** T11, T19

**Files:** `data/*.desktop`, `scripts/build_appimage.sh`, `scripts/render_png_icons.py`, `scripts/check.sh`, `pyproject.toml`

**Scope:** M

### Task 21: README and acceptance pass

**Description:** `README.md` covers install, GUI and CLI usage, screenshot placeholders, how detection works, the safety guarantees, the responsiveness design and future ideas. Then walk through every spec §10 criterion and record the evidence in plan.md.

**Acceptance criteria:**
- [ ] The README covers spec §10 and §11
- [ ] Every spec §10 criterion has recorded evidence (command output or test name)
- [ ] The full gate passes; CI is green

**Dependencies:** T20

**Files:** `README.md`, `tasks/plan.md`

**Scope:** S

## Checkpoint E: Complete
- [ ] All spec §10 acceptance criteria are met, from a clean `pipx install` and from the AppImage
- [ ] Ready for review
