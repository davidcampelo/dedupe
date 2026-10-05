# Implementation Plan: Dedupe

Spec: [docs/duplicate-finder-prompt.md](../docs/duplicate-finder-prompt.md). Detailed tasks: [todo.md](todo.md).

## Overview

A Linux desktop app (Python 3.12+, PySide6) that finds byte-identical duplicate files and hidden/temp files in a folder, recommends which copies to remove, and deletes them safely. Trash is the default, and nothing is deleted without explicit confirmation. The engine (`dedupe/core`) is GUI-free and fully tested. A CLI (`dedupe`) and a Qt GUI (`dedupe-gui`) both sit on top of it. **The GUI never freezes:** scanning, hashing, thumbnailing and deleting all run off the GUI thread, and this is enforced by tests (see "Responsiveness").

## Decisions (confirmed with the user)

| Topic | Decision |
|---|---|
| App ID | `io.github.davidcampelo.Dedupe` (`.desktop` file, icons, `QApplication.setDesktopFileName`, `QSettings` org/app) |
| Writing settings | `tomli-w` is an approved dependency |
| Python | **3.12 minimum**: `requires-python = ">=3.12"`, ruff `target-version = "py312"`, mypy `python_version = "3.12"`. 3.12 syntax (`type` aliases, PEP 695 generics) is allowed |
| Permanent delete | Confirmed with a **checkbox** ("I understand these files cannot be recovered"). OK stays disabled until it's ticked |
| Responsiveness | The GUI must never freeze during a search or any other long operation. This is a hard acceptance criterion with an automated gate |

## Dependency graph

```
models.py (FileEntry, DuplicateGroup, ScanOptions, Recommendation, Progress, CancelToken)
  ├── scanner.py ──┐
  ├── cache.py ────┼── hasher.py ── grouper.py ──┐
  ├── hidden.py    │                             ├── pipeline.py ── cli.py
  ├── recommender.py ────────────────────────────┤
  ├── settings.py (TOML, XDG paths) ─────────────┤
  └── actions.py (plan/guards, trash/delete/hardlink, log)
                                                 │
gui/icons.py (SVGs redrawn from docs/ mockups)   │
gui/workers.py (generic Job runner on QThreadPool + watchdog)
  └── main_window.py
        ├── duplicates_view.py ── image_compare.py (thumbnails.py)
        ├── hidden_files_view.py, skipped_view.py
        └── delete_dialog.py, settings_dialog.py
packaging: .desktop, hicolor icons, AppImage script, README
```

## Architecture decisions

- **One pipeline entry point.** `core.pipeline.run_scan(root, options, progress, cancel) -> ScanResult` drives scanner → grouper → recommender. The CLI, the GUI worker and the tests all call it, so there is exactly one code path to test.
- **Progress and cancellation are plain Python.** The core takes a `CancelToken` (wrapping `threading.Event`) and a progress callback that receives `Progress(stage, done, total, current_path)`. Every directory entry and every 1 MiB chunk checks the token, which keeps cancel latency well under 1 s.
- **Grouper as a strategy chain.** Each stage is a `GroupingStage` (`group(candidates) -> list[list[FileEntry]]`): size, hardlink-collapse, partial hash, full hash and the optional byte compare. A future perceptual-similarity grouper plugs in as another stage (spec §11).
- **Hashing.** xxhash `xxh3_128` over size + first 64 KiB + last 64 KiB for the partial hash. BLAKE3 over 1 MiB chunks for the full hash. Both run in a `ThreadPoolExecutor` (`min(4, cpu_count)` workers). The cache key is `(path, size, mtime_ns, inode, device)`. SQLite runs in WAL mode, with a single writer thread fed by a queue.
- **Actions are a two-phase plan.** `plan_actions(selection, mode) -> ActionPlan` refuses (rather than warns) on the last-copy guard, protected paths and changed-since-scan files. `execute(plan, dry_run, progress, cancel)` re-checks each file immediately before acting. Dry run uses the same planner and skips only the mutation. Hardlink replacement links to a temp name and then `os.replace`s it over the duplicate. If Trash fails, the file is reported as failed. There is **never** a fallback to permanent delete.
- **Settings.** User settings live in `$XDG_CONFIG_HOME/dedupe/settings.toml`: read with `tomllib`, written with `tomli-w`, and validated against a dataclass (unknown keys warn, wrong types fail). UI state (geometry, splitters, recent folders) lives in `QSettings`.
- **Duplicates view.** A flat `QAbstractTableModel` in a `QTableView` that emulates the tree (see "Recorded measurements"), not a `QTreeView`.
- **Icons.** The `docs/` PNGs are design mockups, so the SVGs are redrawn: 18 UI icons on a 24 px grid with a 1.75 stroke and round caps, plus 5 status badges and the "Twin Sheets" app icon (scalable, symbolic, and PNG sizes 16–256 under `io.github.davidcampelo.Dedupe`). Monochrome icons use `currentColor` and are recolored from the palette, so they follow light/dark mode.
- **CLI uses `argparse`.** The CLI has two subcommands and never deletes, so Click isn't worth the extra dependency.
- **Theme.** The app uses Qt 6's native style and palette and follows `QStyleHints.colorScheme()`, updating at runtime when the system theme changes.

## Responsiveness: the GUI never freezes

**Rule:** the GUI thread only does UI work. Every filesystem access, hash, image decode, database call or process lookup runs in a worker. Each long operation goes through one generic `Job` abstraction in `gui/workers.py`: a `QRunnable` on a `QThreadPool` that owns a `CancelToken` and emits `progress`, `finished(result)` and `failed(error)` via queued signals.

| Operation | Where it runs | How results reach the UI |
|---|---|---|
| Duplicate scan (walk, hash, group, recommend) | `ScanJob` | Progress throttled to ≤ 20 Hz; result delivered once at the end |
| Hidden/temp scan | `HiddenScanJob` | Same |
| Deletion / trash / hardlink execution | `ActionJob` (planning and execution) | Per-file progress; cancel stops between files; summary at the end |
| Re-verify before delete (stat of thousands of files) | inside `ActionJob` | Never `stat()` on the GUI thread |
| Thumbnails, EXIF, image metadata | `ThumbnailJob` pool (bounded, lowest-priority) | Placeholder shown at once; image swapped in when ready |
| Cache clear, settings-triggered rescans | Job | Busy indicator on the button only |
| Open file/folder | `QDesktopServices.openUrl` / detached `QProcess` | Fire-and-forget, never `subprocess.run` |
| Populating large models (100k rows) | GUI thread, in **batches** (`beginInsertRows` in chunks via `QTimer.singleShot(0)`) | Each batch takes < 16 ms of work |

**While a job runs**, controls that would conflict (Scan, Delete) are disabled. Cancel, tab switching, scrolling, resizing and the details panel stay usable. Closing the window cancels running jobs and waits for them (bounded at 2 s).

**Enforcement:**
- `tests/gui/conftest.py` provides a `ui_watchdog` fixture. A 10 ms `QTimer` heartbeat on the GUI thread records the longest gap between ticks, and the test fails if any gap exceeds **100 ms** while a job is running or a model is loading. Every GUI task's acceptance criteria use it.
- A static test (`tests/test_gui_thread_rules.py`) parses `dedupe/gui/**` with `ast` and fails on blocking calls outside `workers.py` and the job modules: `os.scandir/walk/stat`, `open()` on user files, `hashlib/blake3`, `subprocess.run`, `send2trash`, `PIL.Image.open`, `sqlite3.connect`. A short allowlist (e.g. reading the bundled icons) lists a reason for each entry.
- Setting `DEDUPE_STALL_LOG=1` installs the same heartbeat in the running app and logs any stall over 100 ms with a stack sample, for manual checks against `/files`.
- At Checkpoints C, D and E, a 100k-file synthetic tree must pass the watchdog end to end (scan → load model → filter → delete as a dry run).

## Quality gates and hooks

All gates run one script, [scripts/check.sh](../scripts/check.sh), so local, hook and CI results agree. **This is already in place (Task 0).**

| Layer | Trigger | What runs | Effect on failure |
|---|---|---|---|
| Claude Code `PostToolUse` | Claude writes or edits a `.py` file | `ruff format` + `ruff check --fix` on that file | Remaining errors are fed back to Claude immediately |
| Claude Code `PreToolUse(Bash)` | Any Bash command | Guard against `--no-verify`, `SKIP_CHECKS`, or repointing `core.hooksPath` | The command is blocked |
| Claude Code `Stop` | Claude tries to end a turn with uncommitted `.py`/`pyproject.toml` changes | `scripts/check.sh --fast` | Claude must keep working (blocks once per stop to avoid loops) |
| git `pre-commit` | `git commit` | `scripts/check.sh --fast` (ruff, format, mypy, pytest excluding `slow`) | The commit is rejected |
| git `pre-push` | `git push` | `scripts/check.sh` (full: adds slow tests and ≥ 90 % `dedupe.core` coverage) | The push is rejected |
| GitHub Actions CI | push / PR | `scripts/check.sh` on Ubuntu 24.04, Python 3.12, Qt offscreen | Red build |
| Checkpoint review | End of each phase | Full gate + manual checklist + human review | No progress to the next phase |

Built into the test suite from Task 1 onwards, so every gate enforces them:
- **Test isolation:** an autouse fixture points `HOME` and every `XDG_*` variable at `tmp_path`, so no test can touch the real Trash, cache, config or action log.
- **Deletion audit:** an `ast` test fails if `send2trash`, `os.unlink/remove/rmdir`, `shutil.rmtree`, `Path.unlink` or `os.replace` appear outside `core/actions.py` and an explicit allowlist (cache/thumbnail housekeeping, each with a reason).
- **GUI-thread rules:** the static test and the watchdog above.
- **Guard mutation checks:** for each safety guard in `actions.py`, a test proves the suite fails when that guard is disabled (Checkpoint B).

**Per-task Definition of Done:** acceptance criteria met; `scripts/check.sh --fast` passes; new behavior has tests that failed before the change; GUI tasks pass the `ui_watchdog`; one commit per task (Conventional Commits) and its box ticked in `todo.md`.

## Task list

### Phase 0: Gates
- [x] T0: Quality gates and hooks (check script, git hooks, Claude Code hooks, CI)

### Phase 1: Headless engine slice
- [x] T1: Project scaffold, models, tooling, test isolation, deletion-audit test
- [x] T2: Scanner (walk, symlinks, exclusions, skipped list, cancel)
- [x] T3: Hasher + grouper (size → hardlink → partial → full → paranoid, empty files)
- [x] T4: `run_scan` pipeline + `dedupe scan --json` CLI

**Checkpoint A:** `dedupe scan` gives correct groups on fixtures and on `/files`; the full gate passes.

### Phase 2: Complete the core
- [x] T5: SQLite hash cache + clear cache
- [x] T6: Recommender + bulk-rule helpers
- [x] T7: Hidden/temp detector + `dedupe hidden` CLI
- [x] T8: Settings (TOML, XDG paths, validation)
- [x] T9: Actions, part 1: plan, guards, dry run, trash, log, progress/cancel
- [x] T10: Actions, part 2: permanent delete + hardlink replacement

**Checkpoint B:** the core is feature-complete and mypy-strict clean; every safety guard is mutation-checked; a 100k-file benchmark has been recorded.

### Phase 3: GUI vertical slice
- [x] T11: Icon set (SVG authoring + theme-aware loader)
- [x] T12: Job runner, UI watchdog, GUI-thread rules test, main window shell with scan/progress/cancel
- [x] T13: Duplicates tree model/view (batched loading, checkboxes, badges, Space toggling)
- [x] T14: Delete confirmation flow (checkbox for permanent delete) executed by `ActionJob`

**Checkpoint C:** scan → review → Trash → log works end to end in the GUI; the 100k-file watchdog run passes; cancel responds within 1 s.

### Phase 4: GUI completeness
- [x] T15: Duplicates tab extras: sort, filters, context menu, details panel, bulk rules
- [x] T16: Hidden & Temp tab + Skipped/Errors tab
- [x] T17: Thumbnail jobs + side-by-side image comparison + preview window
- [x] T18: Thumbnail grid browse mode
- [x] T19: Settings dialog, persisted UI state, theme handling

**Checkpoint D:** every spec §7 feature works by hand; keyboard navigation works; the 100k-file watchdog run passes with every tab populated.

### Phase 5: Ship
- [x] T20: Packaging: `.desktop`, hicolor icons, `pipx install .`, AppImage script (extends the full gate)
- [x] T21: README + acceptance pass against spec §10

**Checkpoint E:** every spec §10 acceptance criterion is met, verified from a clean `pipx install` and from the AppImage.

### Parallelization
After T4, these tasks are independent and can run in parallel: T5, T7 and T8. T6 depends only on models. T11 can run any time after T1. T16, T17 and T19 can run in parallel after T14.

## Recorded measurements

**Checkpoint B benchmark** (`python scripts/bench.py`, 100,000 synthetic files, ~half are copies; 6-core devcontainer, overlay filesystem):

| Run | Time | Notes |
|---|---|---|
| Cold (empty cache) | 36.9 s | 14,800 duplicate groups, 1.58 GB reclaimable |
| Warm (cache populated) | 4.8 s | zero bytes hashed; the time is the walk, grouping and recommending |
| Cancel latency | 0.04 s | from the token firing to `run_scan` returning |

Profiling found three early bottlenecks that were fixed before this record: one future per file (now batched), one SQLite query per file (now bulk `get_many`), and a per-file folder-name check in the recommender (now cached per folder). Warm time went from 16.6 s to 4.8 s.

**Checkpoint C responsiveness** (100,000 synthetic files, `tests/gui/test_scale.py` and an offscreen end-to-end run; longest gap between 10 ms GUI heartbeats):

| Phase | Longest GUI gap | Limit |
|---|---|---|
| Scan (walk, hash, group, recommend) | 22 ms | 100 ms |
| Loading 50k groups into the model | 28 ms | 100 ms |
| Dry-run delete of 50k files (plan + execute + summary) | 39 ms | 100 ms |
| Cancel a running 100k scan | < 1 s (asserted) | 1 s |

Things the watchdog found that shaped the design (all fixed, all covered by the slow gate):
- **QTreeView cannot scale to this.** One layout of 32.5k group rows cost 390 ms because Qt calls the Python model for every row on every change. The duplicates view is therefore a `QTableView` over a flat model that emulates a tree (expand/collapse inserts and removes one group's rows) and a delegate that draws the branch arrows. Loading 100k files went from 11.7 s with 400 ms freezes to 0.7 s with 18 ms gaps.
- **Cyclic GC pauses** (gen 2, 50 to 130 ms with 100k live objects) froze the GUI. Jobs run with the collector paused and the model freezes freshly built nodes (`gui/gcutil.py`); the model breaks node cycles in small slices when it is replaced.
- **GIL starvation.** The default 5 ms switch interval let pure-Python workers starve the GUI thread, so the app sets 0.2 ms (`tune_runtime`), and long worker loops in `actions.py` yield briefly (`_cooperate`). A 100k-entry `sorted()` of identities in the grouper held the GIL for 140 ms and now sorts only multi-link sets.
- **Heavy GUI-thread work moved to workers:** result totals, the post-delete group list and the summary counters are computed in the job, not in the slot.
- Coverage tracing slows Python 2 to 3x, so the full gate (which runs under `--cov`) is the strictest place these tests run; they pass there.

**Similar images** (`python scripts/bench_similar.py`, generated JPEGs of which a quarter have a resized, re-compressed near-copy; 6-core devcontainer, 4 hash workers; "longest stall" is a 10 ms heartbeat thread running beside the scan, the GIL pressure a GUI would feel; the limit is 100 ms):

| Images | First scan | Rescan (cached) | Longest stall | Similar groups |
|---|---|---|---|---|
| 10,000 | 34.5 s | 2.1 s | 19 ms | 2,500 |
| 50,000 | 194 s | 31.2 s | 66 ms (55 ms without the similar pass) | 12,500 |

- **Candidate search** (`--synthetic 50000`, random hashes, bound 12, every pair against 8 variants): 29.9 s for 1.25 billion pairs. It is a small part of the first scan, so a BK-tree or multi-index hashing is not worth adding yet.
- **ThreadPool or ProcessPool: ThreadPool stays.** Pillow releases the GIL while decoding and the 32x32 DCT is tiny; the stall is within the limit and the same as the baseline scan's.
- A rescan of 50k images still takes 31 s although nothing is decoded; the candidate search above accounts for about 30 s of it. The verify step was not timed on its own.

## Acceptance pass (spec section 10)

Run on 2026-10-05 in the devcontainer (no display, no remote, `/files` is a dangling symlink).

| Criterion | Evidence |
|---|---|
| Runnable with `pipx install .`, then `dedupe-gui` and `dedupe` | `pipx install .` into a throwaway `PIPX_HOME` installs both commands. From that install: `dedupe --version` prints `dedupe 0.1.0`; `dedupe scan` finds the expected groups; `python -m dedupe --self-test` (builds the main window offscreen and checks every icon) prints `dedupe self-test: ok`. `tests/test_packaging.py::test_dedupe_gui_answers_help_and_version_without_a_display`. **Not done:** opening the window on a real display (none here). |
| `README.md`: install, usage, screenshots placeholders, how detection works, safety guarantees | [README.md](../README.md): sections Install, Using the GUI, Using the CLI, How detection works, Safety guarantees, Responsiveness, Future ideas; screenshot placeholders under `docs/screenshots/` |
| `.desktop` entry, SVG icon, AppImage build script | `data/io.github.davidcampelo.Dedupe.desktop` (validated by `desktop-file-validate` inside `scripts/check.sh`), `data/icons/hicolor/**` (scalable and symbolic SVG, PNG 16 to 256), `scripts/build_appimage.sh`. `scripts/build_appimage.sh` output: `built dist/Dedupe-0.1.0-x86_64.AppImage (100 MiB); --version and the GUI self-test passed from the AppImage` |
| All tests pass; ruff clean | `scripts/check.sh` (full): ruff check and format clean, mypy `Success: no issues found in 35 source files`, `345 passed`, core coverage `95.42 %` (gate: 90 %), `desktop-file-validate` passes |
| The UI stays responsive on a large folder; cancel works within about a second | `tests/gui/test_scale.py` (100,000 files): scan, load, dry-run delete and every-tab runs stay under the 100 ms watchdog (longest gaps 22, 28 and 39 ms, see "Recorded measurements"); `test_cancel_responds_within_a_second_on_100k_files` asserts cancel under 1 s; `tests/gui/test_main_window.py::test_slow_scan_cancels_within_a_second_without_stalling` |
| No code path deletes a file without explicit user confirmation, and Trash is the default | `tests/test_deletion_audit.py` (deletion calls only in `core/actions.py`, plus `core/settings.py` for its own temp file); GUI: `test_rejecting_the_dialog_leaves_everything_intact`, `test_permanent_needs_the_checkbox_and_switching_modes_clears_it`, the CLI has no deletion code (audit); core: planner refusals and `test_each_guard_is_load_bearing` plus the manual mutation checks recorded under Checkpoint B; defaults: `test_trash_is_the_default_delete_mode`, `test_dialog_defaults_to_trash_and_lists_the_selection` |

**Not verifiable here, so left for a human:** CI on GitHub (no remote), running the real window on a real display with real photos (including the `/files` collection and `DEDUPE_STALL_LOG=1`), approval of the icon contact sheet, and review of the screenshots to add to the README.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| A deletion bug destroys user data | High | Two-phase plan/execute; recheck immediately before each mutation; last-copy guard in core; no Trash→permanent fallback; deletion-audit test; mutation-checked guards; tests isolated from the real `HOME`. |
| The GUI freezes during a scan or delete | High | The `Job` abstraction for all I/O; throttled progress; batched model inserts; `ui_watchdog` in every GUI test; the static GUI-thread rules test; 100k-file watchdog runs at checkpoints. |
| Hardlink replacement leaves a path missing after a crash | High | Link to a temp name, then `os.replace` (atomic within one fs); same-device check; temp files cleaned up on failure. |
| GIL contention makes the UI sluggish while hashing | Med | blake3 releases the GIL on large buffers; the hash pool is capped at `min(4, cpu)`; the thumbnail pool has lower priority; the watchdog catches regressions. |
| `send2trash` fails on other mounts | Med | Reported per file and the file kept; shown in the summary. |
| Qt doesn't run headless in the container or CI | Med | `QT_QPA_PLATFORM=offscreen` is set by `check.sh`; the T12 smoke test proves it early. |
| Icon fidelity when redrawing from PNG mockups | Low | Render a contact sheet next to the mockup for human review (T11). |
| The `Stop` hook slows down turns | Low | It runs only when Python files changed, uses the fast gate, and blocks at most once per stop. |
