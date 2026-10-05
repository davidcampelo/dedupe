# Build "Dedupe": a Linux desktop app for finding duplicate files and cleaning hidden files

You are building a complete, working desktop application for Linux from scratch. Read this whole spec first, then propose a short implementation plan (modules, milestones) before writing code. Build incrementally: get the core engine working and tested before the GUI, and keep the app runnable at the end of every milestone.

## 1. Goal

The user picks a folder. The app scans it recursively, finds files that are byte-for-byte identical, groups them, and recommends which copies can be deleted while keeping one. The user reviews the recommendations (with side-by-side thumbnails for images) and deletes safely. A second tool finds hidden and temporary files (names starting with `.` or `~`, or ending with `~`) and helps the user remove them.

Safety is the top priority: the app must never delete anything the user hasn't explicitly confirmed, and by default deleted files go to the Trash, not permanent removal.

## 2. Tech stack (use this)

- **Language:** Python 3.11+
- **GUI:** PySide6 (Qt 6), native-looking on GNOME and KDE, with light/dark theme following the system
- **Hashing:** `blake3` (fast, native bindings); `xxhash` for the cheap partial-hash pass
- **Images:** Pillow for thumbnails and metadata (resolution, EXIF date); `pillow-heif` optional for HEIC
- **Safe deletion:** `send2trash` (follows the freedesktop.org Trash spec)
- **Cache:** SQLite via the standard `sqlite3` module, stored in `$XDG_CACHE_HOME/dedupe/` (default `~/.cache/dedupe/`)
- **Settings:** `$XDG_CONFIG_HOME/dedupe/settings.toml`
- **Packaging:** `pyproject.toml` (hatchling or setuptools), runnable via `pipx install .`, a `.desktop` file and icon, plus a script that builds an AppImage
- **Tests:** pytest, plus pytest-qt for a few GUI smoke tests
- **Lint/format:** ruff, with type hints throughout (mypy-clean on the core package)

Rationale: Qt gives a mature desktop toolkit with good tree/table views and image handling; the work is I/O-bound, so Python is fast enough when hashing uses native libraries and runs off the UI thread.

## 3. Architecture

Keep the scanning engine fully independent of the GUI so it can be tested and also used from a CLI.

```
dedupe/
  core/
    scanner.py       # walks the tree, collects FileEntry records
    hasher.py        # partial + full hashing, with cache
    grouper.py       # size -> partial hash -> full hash -> (optional) byte compare
    recommender.py   # decides which file to keep in each group
    hidden.py        # hidden/temp file detection
    actions.py       # trash / permanent delete / hardlink replacement, with an action log
    cache.py         # SQLite hash cache
    models.py        # dataclasses: FileEntry, DuplicateGroup, Recommendation, ScanOptions
  gui/
    main_window.py
    duplicates_view.py
    image_compare.py
    hidden_files_view.py
    workers.py       # QThread/QRunnable workers with progress + cancel signals
  cli.py             # `dedupe scan PATH --json` for headless use and testing
tests/
```

## 4. Duplicate detection pipeline

Two files are duplicates only if their contents are identical. Names and dates are never enough. Use a staged pipeline so most files are eliminated cheaply:

1. **Walk** the chosen folder with `os.scandir` (iteratively, not recursively, to avoid recursion limits). Collect path, size, mtime, inode, device, and permissions.
   - Do not follow symlinks by default (option to enable, with loop protection via (device, inode) tracking).
   - Skip files that can't be read and record them in a "skipped" list with the reason (permission denied, vanished, etc.); never crash on them.
   - Don't cross filesystem boundaries by default (option to allow).
   - Respect user-configurable exclusions: glob patterns and folders (defaults: `.git`, `node_modules`, `__pycache__`, `.cache`, `/proc`, `/sys`, `/dev`).
   - Options for minimum file size (default 1 byte) and whether to include hidden files in the duplicate scan.
2. **Group by size.** Files with a unique size cannot have duplicates; drop them.
3. **Hard links:** files sharing (device, inode) are the same file, not duplicates. Collapse them and show them as "already hard-linked" rather than recommending deletion (deleting one frees no space).
4. **Partial hash:** for remaining candidates, hash the first 64 KiB and the last 64 KiB plus the size (xxhash). Drop files whose partial hash is unique.
5. **Full hash:** BLAKE3 of the full content, read in 1 MiB chunks. Files with equal full hashes form a duplicate group.
6. **Optional paranoid mode:** byte-by-byte comparison within each group before marking files as duplicates.
7. **Empty files** (size 0) are reported in their own category, not as one giant duplicate group.

**Hash cache:** store (path, size, mtime_ns, inode, device) → (partial_hash, full_hash) in SQLite. Reuse a cached hash only when all key fields match. Re-scans of large folders should then be near-instant. Provide a "Clear cache" action.

**Performance:** hash in a thread pool (configurable workers, default `min(4, cpu_count)`), stream files rather than loading them into memory, and target scanning 100,000 files without the UI freezing. Report progress per stage (files found, bytes hashed / total, current file) and support cancellation at any point.

## 5. Recommendations: which copy to keep

For each duplicate group, mark exactly one file as **Keep** and the rest as **Delete (suggested)**. Never suggest deleting every copy. Apply these rules in order, and show the reason in the UI (e.g. "Kept: in a preferred folder"):

1. A file inside a user-marked **"Protected / Preferred" folder** wins. Files in protected folders are never suggested for deletion.
2. Avoid keeping copies in folders that look disposable: `Downloads`, `tmp`, `Trash`, `cache`, `backup`, `copy`, `old`.
3. Prefer the file whose name doesn't look like a copy: penalize patterns like `copy`, `Copy of`, `(1)`, `(2)`, `_1`, `- Copy`, `.bak`.
4. Prefer the oldest modification time (likely the original).
5. Prefer the shorter path (less deeply nested).
6. Tie-break alphabetically for determinism.

The user can override any choice per file (Keep / Delete) and apply bulk rules ("keep the newest in all groups", "keep files in folder X", "select all suggested").

## 6. Hidden and temporary file cleaner

A separate tab that scans the same folder for:

- **Dot files/folders:** names starting with `.`
- **Tilde files:** names starting with `~` (e.g. Office lock files like `~$report.docx`) or ending with `~` (editor backups like `notes.txt~`)
- Optionally other temp patterns: `*.swp`, `*.swo`, `.DS_Store`, `Thumbs.db`, `desktop.ini`, `*.tmp`

Show each match with path, size, type (file/folder), and a category label. Important safety rules:

- Many dotfiles are essential configuration (`.bashrc`, `.ssh`, `.config`, `.gnupg`, `.local`, `.git`). Maintain a built-in **protected list** that is shown but unchecked and requires an extra confirmation to delete. Warn prominently if the user selects anything inside their home directory's top-level dot folders.
- Nothing is pre-selected except clearly disposable items (`*~`, `~$*`, `.DS_Store`, `Thumbs.db`, `*.swp` when no matching editor process holds them, if detectable).
- Show total reclaimable space for the current selection.

## 7. GUI

**Main window**
- Folder picker (button + drag-and-drop of a folder onto the window) and a recent-folders list.
- "Scan" button, progress bar with stage text, and "Cancel".
- Tabs: **Duplicates**, **Hidden & Temp Files**, **Skipped / Errors**.
- Status bar: files scanned, duplicate groups, wasted space, selected-for-deletion size.

**Duplicates tab**
- Grouped tree view: one expandable row per group (hash prefix, file size, number of copies, reclaimable space), children are the files with checkbox, path, size, modified date, Keep/Delete badge, and the recommendation reason.
- Sort groups by reclaimable space (default), size, or count. Filter by file type (images, video, audio, documents, archives, other) and by path text.
- Right-click: open file, open containing folder (`xdg-open`), copy path, mark as Keep, mark folder as Protected.
- A details panel for the selected file: full path, size, dates, permissions, full hash.

**Image comparison**
- When a group contains images (jpg, png, gif, webp, bmp, tiff, heic if available), show the copies **side by side as thumbnails** in the details panel, each with resolution, file size, modified date, EXIF date, and its Keep/Delete toggle.
- Double-click a thumbnail for a larger preview window.
- Generate thumbnails in a background thread and cache them (memory LRU plus `$XDG_CACHE_HOME/dedupe/thumbs/`). Optionally reuse the freedesktop thumbnail cache (`~/.cache/thumbnails/`) when present.
- The grouped view can also show a grid-of-thumbnails mode for browsing many image groups quickly.

**Deletion flow**
- "Delete selected" opens a confirmation dialog listing the count, total size, and the first N paths, with a choice of:
  - **Move to Trash** (default)
  - **Delete permanently** (requires typing or ticking an explicit confirmation)
  - **Replace with hard links** (only offered when all files are on the same filesystem; keeps every path working while freeing space)
- Before acting, re-verify each file still exists and its size/mtime is unchanged since the scan; skip and report any that changed.
- Never delete the last remaining copy of a group, even if the user selected every file; block that with a clear message.
- Write every action to a log (`$XDG_DATA_HOME/dedupe/actions.log`) with timestamp, action, path, size, and hash, and show a summary afterwards.
- A **Dry run** toggle that shows exactly what would happen without touching anything.

**Settings dialog**: exclusions, protected folders, minimum size, follow symlinks, cross filesystems, include hidden files in duplicate scan, paranoid byte-compare, worker threads, default delete mode, clear cache.

Remember window size, splitter positions, and last folder between sessions. Support keyboard navigation (arrows, Space to toggle, Delete key opens the confirmation dialog).

## 8. CLI

`dedupe scan PATH [--min-size N] [--exclude GLOB]... [--json]` prints duplicate groups and recommendations. `dedupe hidden PATH [--json]` lists hidden/temp files. The CLI never deletes; it's for scripting and testing.

## 9. Testing

Write tests for the core engine using temporary directories built by fixtures:

- Identical content with different names/dates is detected; same size with different content is not; files that differ only in the middle (same head and tail) are correctly separated by the full hash.
- Hard links are not reported as deletable duplicates; symlinks are skipped by default; symlink loops don't hang.
- Unreadable files go to the skipped list without aborting the scan.
- Cache: a second scan reuses hashes; modifying a file invalidates its entry.
- Recommender: each rule, protected folders, and that at least one copy is always kept.
- Hidden detector: `.x`, `~x`, `x~`, `~$x.docx` are classified correctly; protected dotfiles are flagged.
- Actions: trash, permanent delete, and hardlink replacement on temp files; the last-copy guard; the changed-since-scan guard; dry run touches nothing.
- A pytest-qt smoke test that opens the main window, runs a scan on a fixture folder, and finds the expected groups.

## 10. Deliverables and acceptance criteria

- Runnable with `pipx install .` then `dedupe-gui` (GUI) and `dedupe` (CLI).
- `README.md` covering install, usage, screenshots placeholders, how detection works, and the safety guarantees.
- `.desktop` entry and an SVG icon; AppImage build script.
- All tests pass; ruff clean.
- The UI stays responsive during a scan of a large folder, and cancel works within about a second.
- No code path deletes a file without an explicit user confirmation, and Trash is the default.

## 11. Out of scope for v1 (note as future ideas in the README)

Perceptual "similar image" detection (e.g. pHash via `imagehash`), audio/video similarity, scanning multiple root folders at once, and scheduled scans. Design `grouper.py` so a similarity-based grouper could be added later.
