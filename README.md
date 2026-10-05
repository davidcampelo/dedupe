# Dedupe

Find duplicate files and clean up hidden and temporary files on Linux, **safely**.

Pick a folder. Dedupe scans it, groups files whose contents are byte-for-byte identical, recommends
which copy to keep, and moves the rest to the Trash after you confirm. A second tool lists hidden
and temporary files (`.cache`, `~$report.docx`, `notes.txt~`, `*.swp`, `.DS_Store` ...). Images are
compared side by side. Optionally, it also finds images that are *almost* the same (resized,
re-compressed, converted, rotated or mirrored copies, and shots of the same scene taken seconds
apart); see [Similar images](#similar-images).

> Screenshots: _placeholders, to be added under `docs/screenshots/`_
>
> | Duplicates | Image comparison | Hidden & Temp |
> |---|---|---|
> | `docs/screenshots/duplicates.png` | `docs/screenshots/compare.png` | `docs/screenshots/hidden.png` |

## Install

Requires Python 3.12+ and the usual Qt runtime libraries (on Debian/Ubuntu:
`libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0`).

```bash
pipx install .             # from a checkout; gives you `dedupe` and `dedupe-gui`
pipx install ".[heif]"     # optional: HEIC/HEIF thumbnails (pillow-heif)
pipx install ".[similar]"  # optional: similar-image search (imagehash, numpy, scipy)
pip install "dedupe[similar]"   # the same extra from a package index
```

Without the `similar` extra everything else works; the similar-image controls are greyed out with
"install dedupe[similar]" and `dedupe scan --similar` exits with code 2 and the same hint. The
extra pulls in numpy, scipy and PyWavelets, which is why it is optional.

Or build a single-file AppImage (Linux x86_64; needs network on the first build):

```bash
pip install -e ".[dev]"
scripts/build_appimage.sh  # writes dist/Dedupe-<version>-x86_64.AppImage
dist/Dedupe-*.AppImage                          # opens the GUI
dist/Dedupe-*.AppImage scan ~/Pictures --json   # the same file is also the CLI
dist/Dedupe-*.AppImage scan ~/Pictures --similar
```

The AppImage bundles the `similar` extra (and `heif`), so it is larger than a plain install, mostly
because of scipy.

The script checks the built AppImage itself: it runs `--version` and a headless GUI self-test from
it, and unpacks it to confirm the desktop file and icon are inside. `pipx install` also ships the
`.desktop` file and hicolor icons under the venv's `share/`; copy them into
`~/.local/share/applications` and `~/.local/share/icons` if you want a menu entry.

## Using the GUI (`dedupe-gui`)

1. **Choose a folder** (button, drag-and-drop onto the window, or the recent-folders list) and press
   **Scan**. Duplicates and hidden files are both found; progress and **Cancel** are always live.
2. **Duplicates tab.** One expandable row per group (hash prefix, size, copies, reclaimable space);
   the files under it have a checkbox, size, date, a **Keep / Delete / Protected** badge and the
   reason. Files suggested for deletion start ticked; one copy per group is always kept.
   - Sort by reclaimable space, size or copies; filter by file type and by path text.
   - Right-click: open file, open containing folder, copy path, **Mark as Keep**, **Mark folder as
     Protected** (saved, and the recommendations re-run at once).
   - **Bulk rules:** select all suggested, keep the newest copy everywhere, keep copies in a
     folder, deselect everything.
   - The side panel shows the selected file's path, size, dates, permissions and hash. For images
     it shows the copies **side by side** (resolution, size, dates, EXIF date, Keep/Delete toggle;
     double-click for a larger preview). **Grid** mode browses image groups as thumbnails.
3. **Delete selected…** (button or the Delete key) opens a confirmation listing the count, size and
   the first paths, with the mode: **Move to Trash** (default), **Delete permanently** (needs the
   "I understand these files cannot be recovered" box), or **Replace with hard links** (only when
   every file is on one filesystem). **Dry run** shows what would happen and changes nothing.
4. **Hidden & Temp Files tab.** Dot files/folders, `~`/`~$` files, `x~` backups, optional
   `*.swp`, `.DS_Store`, `Thumbs.db`, `desktop.ini`, `*.tmp`. Only clearly disposable items start
   ticked. Protected configuration (`.bashrc`, `.ssh`, `.config`, `.gnupg`, `.local`, `.git` ...) is
   listed but never ticked for you, and ticking it, or anything in a top-level hidden folder of your
   home, asks for an extra confirmation. The reclaimable total follows your selection.
5. **Skipped / Errors tab.** Everything that could not be read, and why. The scan never aborts on it.
   Images that cannot be decoded (corrupt, truncated, too large) are listed here too.
6. **Similar Images tab** (fourth tab; empty unless *Find similar images* is on in Settings). One
   expandable row per group of images that look the same but are different files. Each image shows
   its dimensions, size, how similar it is to the group's reference image, and "+N" when N exact
   copies of it exist elsewhere (those stay on the Duplicates tab). **Nothing is ticked for you:**
   the Suggestion column and the compare panel show which image the recommender would keep (the
   highest resolution, then the larger file), and **Select suggested** ticks the rest only when
   you press it. The delete dialog says "These are similar, not identical. The deleted images'
   content will not exist anywhere else." and has no hard-link option. Deleting on either tab
   updates both.
7. **Settings…** (`Ctrl+,`): exclusions, protected folders, minimum size, follow symlinks, cross
   filesystems, hidden files in the duplicate scan, paranoid byte-compare, worker threads, default
   delete mode, use/clear the hash cache, and **Find similar images** with a Strict / Normal /
   Loose choice. Changes apply from the next scan.

| Keys | |
|---|---|
| `Ctrl+O` / `Ctrl+R` or `F5` / `Esc` | choose folder / scan / cancel |
| `Ctrl+1` ... `Ctrl+4` | switch tab |
| Arrows, `Space`, `Delete` | move, toggle the selected rows, open the delete dialog |
| `Right` / `Left` / `Enter` | expand / collapse a group |

Window size, splitter positions, the last folder and the recent folders are remembered. Light and
dark themes follow the system; icons are recoloured when it changes.

## Using the CLI (`dedupe`)

The CLI never deletes anything; it is for scripting and testing.

```bash
dedupe scan PATH [--min-size N] [--exclude GLOB]... [--protect DIR]... [--json]
                 [--paranoid] [--follow-symlinks] [--cross-filesystems] [--no-hidden] [--no-cache]
                 [--similar [--threshold {strict,normal,loose,N}]]
dedupe hidden PATH [--json] [--no-temp-patterns]
dedupe cache clear
```

`scan --json` prints `root`, `files_scanned`, `reclaimable`, `cancelled`, `groups` (each with
`hash`, `size`, `reclaimable`, `files[]` with `path`, `size`, `mtime_ns`, `verdict`, `reason`, and
`hardlinked[]`), `empty_files`, `hardlink_sets` and `skipped`. With `--similar` it also prints a
`similar_groups` array (each group: `id`, `reclaimable`, and `members[]` with `path`, `size`,
`mtime_ns`, `width`, `height`, `distance`, `similarity`, `aliases[]`, `verdict`, `reason`); without
`--similar` the output has exactly the keys above. The table output gets a "Similar images" section.
`--threshold` is `strict` (4), `normal` (8, the default), `loose` (12) or a number of bits from 0 to
16. Ctrl-C cancels cleanly (exit 130); a bad path exits 2.

## How detection works

Two files are duplicates only if their **contents** are identical; names and dates never matter.
A staged pipeline removes most files cheaply:

1. **Walk** with an iterative `os.scandir` (no recursion limit). Symlinks are not followed unless
   enabled (loops are caught by `(device, inode)`), other filesystems are not entered, and
   `.git`, `node_modules`, `__pycache__`, `.cache`, `/proc`, `/sys`, `/dev` are excluded by default.
   Unreadable entries go to the *skipped* list.
2. **Group by size**: a unique size cannot have a duplicate.
3. **Hard links**: files sharing `(device, inode)` are one file, shown as "already hard-linked".
   Deleting one frees no space, so it is never recommended.
4. **Partial hash**: xxh3 of the size plus the first and last 64 KiB.
5. **Full hash**: BLAKE3, 1 MiB chunks. Equal hashes form a group.
6. **Paranoid mode** (optional): byte-for-byte comparison inside each group.
7. **Empty files** are reported separately, never as one giant group (they only appear when the
   minimum size is set to 0).

A SQLite **hash cache** (`$XDG_CACHE_HOME/dedupe/hashes.db`) is keyed on
`(path, size, mtime_ns, inode, device)`, so a re-scan of an unchanged folder hashes nothing. A
corrupt or locked cache degrades to "no cache" with a warning.

**Which copy is kept**, in order: a file in a protected folder (never suggested for deletion);
not in a disposable-looking folder (`Downloads`, `tmp`, `Trash`, `cache`, `backup`, `copy`, `old`);
a name that doesn't look like a copy (`Copy of`, `(1)`, `_1`, `- Copy`, `.bak`); the oldest
modification time; the shorter path; alphabetical order. The reason is shown next to every file.

## Similar images

Off by default (`similar_images = false`): every image has to be decoded, which costs far more than
hashing bytes. Turn it on in Settings or with `--similar`. It needs the `similar` extra.

It runs as its own chain after the exact-duplicate pass, because similar images almost never have
the same size:

1. **Filter** raster images by extension (not SVG, ICO or camera RAW; HEIC only when pillow-heif is
   installed). Corrupt, truncated and decompression-bomb files go to *Skipped*, never abort a scan.
2. **Collapse exact copies.** Hard links and byte-identical files stand for one image, so no set of
   files appears on both the Duplicates and the Similar tab; the others are listed as that image's
   exact copies.
3. **Perceptual hash.** The image is decoded once, uniform borders (letterboxing) are trimmed and it
   is flattened to a 64x64 grayscale copy. Its **pHash** and **dHash** (64 bits each, from
   [`imagehash`](https://github.com/JohannesBuchner/imagehash)) are computed for the image and for
   its 8 rotations and mirror images, so a copy rotated 90 degrees with no EXIF tag, or mirrored,
   still matches. Flat images (a solid colour) are excluded: their hash would match everything.
   The EXIF capture time is read too, and an image that has one also keeps a 32x32 *sketch* of
   its grayscale copy (1 KiB). Hashes and sketches are cached next to the file hashes, keyed on
   the same stat fields plus the installed `imagehash` and Pillow versions, so an upgrade
   recomputes instead of mixing hash values.
4. **Candidates.** Every pair is compared by Hamming distance (the number of differing bits out of
   64), by brute force in numpy. A pair is a candidate when both its pHash and its dHash are within
   the bound for one of the variants. Shots **taken at most 60 seconds apart** (EXIF capture time)
   are candidates too, whatever their hashes say, each with at most its next 6 shots: when the
   camera moves a little between two shots of the same scene the picture shifts, and a 5% shift
   already moves pHash and dHash 15-25 bits, as far as two unrelated photos.
5. **Verify.** A pair within the threshold on both hashes is accepted. Any other candidate is
   first **aligned**: phase correlation finds the shift (up to 16% of the side) that lines the two
   images up, and SSIM is computed on the part they share. A pair a little further out on the
   hashes (up to 12 bits, or the threshold if larger) needs an SSIM of at least 0.90 on the 64x64
   copies; shots taken seconds apart need 0.80 on their cached sketches, so verifying them never
   decodes an image. Strict is 4 bits, Normal 8, Loose 12.
6. **Cluster** with leader clustering, not connected components: A~B and B~C never put A and C in one
   group when they look nothing alike. Every member matches its group's leader, the output does not
   depend on the order files were found in, and the leader is the best image: highest resolution,
   then the larger file, then the rules used for exact duplicates.

What it does **not** find, deliberately:

| Case | Why not |
|---|---|
| Heavy crops (a small part of the picture) | the global hash changes completely; `imagehash.crop_resistant_hash` is a possible follow-up, but it is much slower and needs its own matching |
| Watermarks and text overlays | they move enough hash bits and SSIM |
| The same scene from another angle, or zoomed | only a shift is aligned, not a change of perspective or scale; that needs AI image embeddings |
| Shots of the same scene without a capture time, or minutes apart | without EXIF time, only the hashes can bring two images together, and they are not shift tolerant |

Measured on generated images (6 cores): 10,000 images take about 35 s the first time and 2 s from
the cache; 50,000 take about 3 minutes and 30 s. Capture times add work only for photos shot close
together: in the worst case (2,000 photos, each 5 s after the last, so every photo has 6 neighbours
to check) the first scan takes 12.5 s and the cached rescan 5 s. See `tasks/plan.md`.

## Safety guarantees

- **Nothing is deleted without your confirmation**, and **Trash is the default**. The only code
  that removes files is `dedupe/core/actions.py`; a test (`tests/test_deletion_audit.py`) fails if a
  deletion call appears anywhere else.
- **Plan first, then act.** The planner *refuses* (it does not just warn) a selection that would
  remove every copy of a group, that touches a protected folder, or that asks for hard links
  across filesystems, before anything changes.
- **Re-verified before every change.** Each file must still be a file of the same type, size,
  modification time and identity as when scanned, and a kept copy must still be unchanged;
  otherwise it is skipped and reported.
- **No fallback to permanent deletion.** If the Trash fails for a file, it is reported and kept.
- **Hard-link replacement is atomic** (link to a temporary name, then rename over the duplicate),
  and a failure leaves no temporary files and no missing paths.
- **Every action is logged** (JSON lines in `$XDG_DATA_HOME/dedupe/actions.log`, opened *before*
  the first change; if the log cannot be opened nothing is touched), and summarised afterwards.
- **Similar images are different files**, so the rules are stricter: hard links are refused outright
  (they would replace one picture with another), at least one member of every group must stay (it
  is re-verified before anything is removed), nothing is pre-selected, and the confirmation says
  that the content of a deleted image will not exist anywhere else. Each refusal has a mutation test.
- **Dry run** goes through the same plan and checks and skips only the change.
- Protected configuration files and folders need an explicit extra confirmation, and the core
  refuses them unless that confirmation was given. The home folder and `/` are never removable.
- The tests run with `HOME` and every `XDG_*` variable pointed at a temporary directory, so they can
  never touch your real Trash, cache, config or log.

## Responsiveness

The GUI thread only does UI work. Scanning, hashing, grouping, deleting, thumbnails and settings
writes run as jobs on a thread pool with cancellation and throttled progress. Large lists load into
the view in small batches and are a flat table, not a `QTreeView` (which lays out every row through
Python on each change). Cyclic-GC pauses and GIL starvation are controlled, and long worker loops
yield. This is enforced by tests: a heartbeat watchdog fails any GUI-thread gap over 100 ms, a
static test fails blocking calls in GUI modules, and 100,000-file runs (scan, load, filter, dry-run
delete, every tab populated) must pass the watchdog. Set `DEDUPE_STALL_LOG=1` to log any stall in
the running app with a stack sample. Measurements are recorded in `tasks/plan.md`.

## Files and locations

| What | Where |
|---|---|
| Settings | `$XDG_CONFIG_HOME/dedupe/settings.toml` (default `~/.config/dedupe/`) |
| Hash cache | `$XDG_CACHE_HOME/dedupe/hashes.db` |
| Thumbnail cache | `$XDG_CACHE_HOME/dedupe/thumbs/` (the freedesktop cache in `~/.cache/thumbnails/` is reused when valid) |
| Action log | `$XDG_DATA_HOME/dedupe/actions.log` |
| Window state | Qt `QSettings` (`davidcampelo/Dedupe`) |

## Development

```bash
pip install -e ".[dev,similar]"
scripts/check.sh --fast   # ruff, ruff format, mypy, pytest (no slow tests): the pre-commit gate
scripts/check.sh          # + slow 100k-file tests, >= 90 % core coverage, desktop-file-validate
python scripts/bench.py   # 100k-file benchmark: cold, warm, cancel latency
python scripts/bench_similar.py --images 10000   # similar-images benchmark (needs the extra)
python scripts/render_icons.py          # icon contact sheet (light and dark)
python scripts/render_png_icons.py      # regenerate data/icons/hicolor
```

Git hooks (`.githooks`), Claude Code hooks (`.claude/`) and CI all run the same
`scripts/check.sh`. The plan and task list live in [tasks/](tasks/).

## Future ideas (out of scope for now)

Audio/video similarity, crop-resistant image matching, scanning several root folders at once, and
scheduled scans. `core/grouper.py` is a chain of stages for exact duplicates; the similar-images
chain in `core/similar.py` runs after it.
