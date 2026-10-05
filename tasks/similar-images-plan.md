# Implementation Plan: Similar Images

Main plan: [plan.md](plan.md). Tasks so far: [todo.md](todo.md). Spec: [docs/duplicate-finder-prompt.md](../docs/duplicate-finder-prompt.md) §11, which lists this feature under future ideas.

**Status:** planned, not started.

## Overview

Find images that are **almost the same** but are not byte-identical. Examples: the same photo resized, re-compressed, converted to another format, slightly brightened, rotated without an EXIF tag, mirrored, or letterboxed. The feature covers images only; audio and video similarity are still out of scope.

Each image gets a **perceptual hash**. Similar images get hashes that differ in only a few bits, so two images are compared by **Hamming distance** (the number of differing bits out of 64), not by equality. A loose hash threshold finds candidate pairs, and a pixel-level check (SSIM) confirms them:

```
original.jpg        1011 0110 1100 0011 …
resized_q60.jpg     1011 0110 1000 0011 …   distance 3  → similar
other_photo.jpg     0100 1011 0011 1101 …   distance 31 → not similar
```

The feature is **off by default** (`similar_images = false`), because decoding every image costs much more than hashing bytes.

## Decisions

| Topic | Decision | Status |
|---|---|---|
| Hash implementation | **`imagehash`** (`phash` + `dhash`), chosen because it is widely used and has years of real-world use. All calls go through one function, `perceptual_hash()` in `core/perceptual.py`, so nothing else imports `imagehash` | **confirmed with the user** |
| Packaging | Optional extra `similar = ["imagehash>=4.3,<5", "numpy>=2.0"]`. `imagehash` pulls in numpy, scipy and PyWavelets; numpy is listed too because the code uses it directly, and 2.0 is needed for `np.bitwise_count`. Without it, the feature is greyed out with "install dedupe[similar]". The AppImage bundles the extra and accepts the extra size (roughly 60–100 MB, mostly scipy) | confirmed |
| Hash stability | Hash values can change when `imagehash` or Pillow is upgraded (the resampling filter has changed before). So the cache key for perceptual hashes includes the **installed `imagehash` and Pillow versions** together with our `ALGO_VERSION`; an upgrade drops stale entries instead of silently mixing hash values | confirmed |
| Default | Off; enabled in Settings or with `--similar` | proposed |
| Threshold | Presets Strict = 4, Normal = 8 (default), Loose = 12 bits out of 64; stored as an int and validated to the range 0–16 | proposed |
| Pre-selection | **Nothing in a similar group is pre-selected for deletion.** The files are different, so the user chooses | proposed |
| Hard links | **Always refused** for similar groups | firm (safety) |

## Where it fits

The `GroupingStage` protocol (buckets in, buckets out) fits as it is. The existing chain does not, because it starts with `SizeStage` and similar images almost never have the same size. Similarity therefore runs as **its own chain**, after the exact-duplicate pass and inside the same `run_scan`:

```
exact chain   : Size → Hardlink → Partial → Full → (ByteCompare)                → groups
similar chain : ImageFilter → Hardlink → CollapseExact → PerceptualHash
                → Candidates → Verify → Cluster                                → similar_groups
```

- **ImageFilter** keeps raster images by extension. The list is `file_types.IMAGES` without `.svg`, `.ico` and the RAW formats, which Pillow can't decode. The list moves into `core`, because `core` must not depend on the GUI.
- **CollapseExact** keeps one file to stand for each exact-duplicate group. The others become its `aliases`, so no set of files ever appears on both tabs.
- **PerceptualHash** decodes each image once and computes the hashes, using the cache.
- **Candidates** finds the pairs whose pHash and dHash are both within the Loose bound.
- **Verify** checks each candidate pair with SSIM and drops the false positives.
- **Cluster** forms the final groups with leader clustering.

## Engine design (`dedupe/core`)

### Decoding: `core/imaging.py`
- One Pillow loader that the engine and `gui/thumbnails.py` both use. `decode_image` is rewritten to call it.
- It opens the file and applies `draft()` (fast JPEG downscale), then `ImageOps.exif_transpose`. For animated images it uses the first frame. It registers pillow-heif when that package is installed.
- `DecompressionBombError`, corrupt files and unsupported formats each become a `SkippedEntry` with a reason. One bad image never fails a scan.

### Hash: `core/perceptual.py`
- **pHash** (`imagehash.phash`: 64-bit, the DCT of a 32×32 grayscale image) and **dHash** (`imagehash.dhash`: 64-bit, from horizontal gradients). Hashes are converted to plain `int`s straight away, so the rest of the engine never handles `ImageHash` objects.
- `imagehash` is imported lazily inside this module. `similar_available()` reports whether the extra is installed, and the CLI and GUI check it.
- **Rotations and flips:** the image is shrunk once to a small working copy, and each of its 8 rotations and mirror images is passed to `phash`/`dhash`. The distance between two images is the minimum over the variants, which catches 90° rotations with no EXIF tag and mirrored copies. This is cheap, because it runs on an array that is already decoded.
- **Border trim:** uniform borders (letterbox, padding) are removed before hashing.
- **Uniform images are excluded:** when the hash bit count is ≤ 2 or ≥ 62, or the pixel variance is near zero, the hash would match everything.
- For each image the code stores `PerceptualHash(phash, dhash, variants, width, height)` and a **64×64 grayscale thumbnail** for the verify step. The thumbnail lives only in memory; the cache stores only the hashes.
- An `ALGO_VERSION` constant covers our own steps (border trim, variants, the uniform filter). The cache stamp is `f"{ALGO_VERSION}:{imagehash.__version__}:{PIL.__version__}"`, so changing any of them invalidates old entries.

### Cache
- Add `perceptual TEXT` and `perceptual_algo TEXT` (the stamp above) columns to the `hashes` table. They are added with `ALTER TABLE … ADD COLUMN` when missing. On failure the cache is disabled with a warning, which is the existing degrade path.
- They use the same five-field key and the same `UPSERT`/`COALESCE` pattern. `HashStore` gains `put_perceptual`, and `get_many` returns the new field.
- A rescan decodes only new or changed images. Images that need the verify step are decoded again on demand.

### Concurrency and progress
- Hashing runs on the existing `HashService` thread pool. Pillow releases the GIL while decoding, and the scipy DCT on a 32×32 array is tiny, so threads should be enough. Measure with `scripts/bench.py`, and switch to a `ProcessPoolExecutor` only if the 100 ms GUI-stall gate fails.
- New stage `Stage.SIMILAR = "image similarity"`, counted in images. The cancel token is checked for every image, every numpy chunk and every verified pair.

### Candidates
- Pairs are compared by brute force in numpy, in chunks of rows: XOR, then `np.bitwise_count`. This is exact and simple; 50k images is about 1.25 billion pairs. Cancel is checked between chunks.
- A pair is a candidate when the minimum pHash and dHash distances over the variants are both ≤ `max(threshold, 12)`, capped at 16.
- A BK-tree or multi-index hashing is only worth adding if the benchmark says so.

### Verify
- A pair is accepted at once when both distances are ≤ `threshold`.
- A pair in the **grey zone** (between `threshold` and the candidate bound) is accepted only when **SSIM ≥ 0.90**. SSIM is our own short function in numpy (installed with the extra), computed on the two 64×64 grayscale images, aligned with the variant that matched best.
- Each accepted pair keeps a **similarity score**, which is SSIM, or 1 − distance/64 when SSIM was not computed. The GUI shows this as "% similar".

### Cluster
- **Leader clustering, not connected components.** Connected components chain matches together: A≈B and B≈C would put A and C in one group even when they look nothing alike.
- Images are sorted best first: largest pixel count, then the recommender order. Each image either joins the first leader it has an accepted pair with, or becomes a new leader.
- **Invariant:** every member has an accepted pair with its group's leader. The output is deterministic and doesn't depend on input order.
- Python loops call `_cooperate()`, as `actions.py` does.

### Models
`DuplicateGroup` is not reused: its `reclaimable = size × (n−1)` is wrong when the files differ in size, and `hash` doesn't identify a similar group.

```python
@dataclass(frozen=True, slots=True)
class SimilarMember:
    entry: FileEntry
    width: int
    height: int
    distance: int  # best pHash distance to the leader (0 for the leader)
    similarity: float  # 0..1, SSIM or derived from distance
    aliases: tuple[Path, ...] = ()  # exact copies collapsed into this member


@dataclass(frozen=True, slots=True)
class SimilarGroup:
    id: str  # stable: hash of the sorted member paths
    members: tuple[SimilarMember, ...]
    recommendations: tuple[Recommendation, ...] = ()
    # reclaimable: the sum of every non-keeper's size
```

- `ScanResult` gains `similar_groups: tuple[SimilarGroup, ...] = ()`.
- `ScanOptions` and `Settings` gain `similar_images: bool = False` and `similarity_threshold: int = 8`.

### Recommender
- The keeper order is: highest resolution, then larger file size (less compression), then the existing rules (not a copy-looking name, not a disposable folder, oldest, shortest path, alphabetical).
- Files in protected folders are never suggested for deletion.
- Verdicts only inform the user; nothing is pre-selected.

## Safety

Similar images are **different files**. Every rule that relies on "the content survives elsewhere" changes:

| Guard | Exact groups (today) | Similar groups |
|---|---|---|
| Hard-link mode | allowed | **refused outright** (it would replace one image with another's content) |
| Last-copy guard | refuse when every copy is selected | refuse when every member, including its `aliases`, is selected |
| Protected folders | refused | refused (same helper) |
| Changed since scan | stat re-check | the same `_verify_unchanged`; the keeper is re-verified |
| Confirmation | standard dialog | adds: **"These are similar, not identical. The deleted images' content will not exist anywhere else."** |

- `plan_similar_actions()` lives in `core/actions.py`, so the deletion audit test is unchanged. It reuses `_check_protected` and `_verify_unchanged`, and it adds `_check_last_copy_similar` and the hard-link refusal.
- Each refusal has a **mutation test**: remove the guard, and the test must fail.
- `similar_groups_after()` mirrors `groups_after()`. Deleting on either tab updates both tabs, including the `aliases`.

## Interfaces

**CLI**
- New flags: `dedupe scan PATH --similar [--threshold {strict,normal,loose,N}]`.
- The table output adds a "Similar images" section.
- `--json` adds a **new** `similar_groups` key. Existing keys are unchanged, so the schema stays backward compatible.
- The CLI still never deletes.

**GUI**
- **A fourth tab, "Similar Images"**, with a new `similar-images.svg` icon on the 24 px grid with the 1.75 stroke.
- A flat table model in the style of `DuplicatesModel`, plus `ImageComparePanel`. Each card adds dimensions, file size, "% similar" and an "+N exact copies" badge.
- Delete goes through `ActionJob` and the stronger dialog wording, with no hard-link option.
- Settings dialog: a "Find similar images" checkbox and a Strict/Normal/Loose combo box.
- Nothing new runs on the GUI thread, and every test runs under `ui_watchdog`.

## Not covered (deliberately)

| Case | Why not | What it would take |
|---|---|---|
| Heavy crops (a small part of the picture) | the global hash changes completely | `imagehash.crop_resistant_hash` is already available with the extra, but it is much slower and needs its own matching step. A good follow-up feature |
| Watermarks or text overlays | they move enough bits and SSIM | the same as above |
| Same scene, different shot (bursts, a different angle) | not "the same image" | AI image embeddings (a CLIP-style model): hundreds of MB, GPU-class work |

## Tasks

**Every task's gate:** `scripts/check.sh --fast`; one commit per task; its box ticked here.
**Checkpoint gate:** full `scripts/check.sh` (slow tests, core coverage ≥ 90 %) and human review.

### Phase S1: Engine

#### Task S1: Shared image loader ✅
`core/imaging.py`; `gui/thumbnails.decode_image` rewritten to use it; the image extension list moved into `core`.
- [x] EXIF-rotated, HEIF (skipped when pillow-heif is absent), corrupt, truncated and decompression-bomb files each produce an oriented image or a `SkippedEntry` with a reason
- [x] Existing thumbnail tests pass unchanged

**Scope:** S

#### Task S2: Perceptual hash ✅
`pyproject.toml`: the `similar` extra; `dev` installs it too, and CI and the devcontainer install `.[dev,similar]`. `core/perceptual.py`: lazy `imagehash` import, `similar_available()`, pHash and dHash, 8 rotation and flip variants, border trim, uniform-image filter, the cache stamp.
- [x] Generated image pairs stay within 8 bits: resized, re-saved as JPEG at quality 60, +10 % brightness, EXIF-rotated, **rotated 90° with no EXIF**, **mirrored**, **letterboxed**
- [x] A gradient vs a checkerboard is more than 20 bits apart; a solid colour image is excluded
- [x] Golden-value test: fixed images give fixed hashes for the pinned `imagehash`/Pillow versions. A failure after an upgrade is the signal to review the cache stamp
- [x] With `imagehash` hidden (monkeypatched import), `similar_available()` is false and nothing else breaks

**Scope:** M

#### Task S3: Cache columns ✅
- [x] An existing database migrates in place; a changed mtime invalidates the entry; a different cache stamp (`ALGO_VERSION`, `imagehash` or Pillow version) invalidates it; a broken database degrades with a warning and the scan still succeeds

**Scope:** S

#### Task S4: Candidates and clustering ✅
Chunked numpy pairwise comparison; leader clustering.
- [x] Hypothesis: every member has an accepted pair with its leader; the output doesn't depend on input order; the candidates match a pure-Python brute-force reference
- [x] Cancel mid-run returns within 100 ms

**Scope:** M

#### Task S4b: SSIM verification ✅
- [x] A grey-zone pair of the same photo (heavily re-compressed and resized) passes
- [x] A grey-zone pair of different photos with a similar layout fails
- [x] A pair already within the threshold skips SSIM

**Scope:** S

#### Task S5: Pipeline, models, recommender ✅
The similar chain inside `run_scan`; `SimilarGroup` and `SimilarMember`; the quality-first keeper order.
- [x] Off by default; with it on, exact copies collapse into `aliases` and never appear in both lists
- [x] A cancelled scan returns no groups; the keeper is the highest resolution; a protected file is never marked DELETE

**Scope:** M

#### Task S6: Actions and guards ✅
`plan_similar_actions`, `_check_last_copy_similar`, the hard-link refusal, `similar_groups_after`.
- [x] Hard links are refused; selecting the last copy (including aliases) is refused; a protected file is refused. Each has a mutation test
- [x] Execution re-checks the keeper and every selected file with `_verify_unchanged`

**Scope:** M

#### Task S7: CLI ✅
- [x] `--similar` and `--threshold` work; the JSON snapshot is byte-identical when `--similar` is off
- [x] Without the extra, `--similar` exits with code 2 and "install dedupe[similar]"

**Scope:** S

### Checkpoint S-A: Engine complete ✅ (ThreadPool stays; the human review is still open)
- [x] Full `check.sh` passes
- [x] Bench on 10k and 50k generated images: first-scan time, rescan time (cache), candidate and verify time; results recorded in [plan.md](plan.md) under "Recorded measurements"
- [x] Decide ThreadPool vs ProcessPool from the GUI-stall numbers

### Phase S2: GUI

#### Task S8: Settings
- [ ] `similar_images` and `similarity_threshold` are validated: a threshold outside 0–16 is rejected, and a bool is rejected where an int is expected
- [ ] The dialog has a checkbox and the Strict/Normal/Loose combo box; when `imagehash` is missing the controls are disabled with "install dedupe[similar]"

**Scope:** S

#### Task S9: Similar Images tab
Tab, model, `ImageComparePanel` additions, icon.
- [ ] 5k groups load with no GUI-thread gap over 100 ms (`ui_watchdog`)
- [ ] Cards show dimensions, size, "% similar" and "+N exact copies"; the icon works in light and dark themes

**Scope:** L

#### Task S10: Delete flow
- [ ] The dialog shows the "similar, not identical" warning and has no hard-link option
- [ ] Deleting on one tab updates the other, including aliases

**Scope:** M

### Checkpoint S-B: Feature complete
- [ ] Full `check.sh` passes; a manual pass on a real photo folder

### Phase S3: Ship

#### Task S11: Docs and packaging
- [ ] README: the feature moves out of "Future ideas", with the limits from "Not covered"
- [ ] README documents `pip install "dedupe[similar]"`
- [ ] The AppImage bundles the `similar` extra; `dedupe scan --similar` works from the AppImage; the new AppImage size is recorded

**Scope:** S

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| False positives (screenshots, scanned documents, bursts) | Both pHash and dHash must match; SSIM checks the grey zone; Normal is the default threshold; images are shown side by side; nothing is pre-selected |
| Deleting a "similar" image the user wanted | Hard links refused; stronger dialog wording; Trash stays the default |
| First-scan cost on large libraries | Opt-in; cache; progress counted in images; cancel checked per image |
| GIL stalls from numpy chunks or the clustering loop | Bounded chunk size; `_cooperate()`; `ui_watchdog` gate; ProcessPool fallback |
| An `imagehash` or Pillow upgrade changes hash values | The cache stamp includes both versions; the golden-value test fails on any change; the extra is capped below the next major version |
| AppImage grows by roughly 60–100 MB (scipy) | Accepted for the maturity of `imagehash`; the size is recorded in S11 |
| Memory from keeping 64×64 grayscale images | 4 KiB per image (200 MB at 50k): keep them only for images that are part of a candidate pair, and decode the rest again on demand |
