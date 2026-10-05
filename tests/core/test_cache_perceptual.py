from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from dedupe.core import paths, perceptual
from dedupe.core.cache import HashCache
from dedupe.core.hasher import HashService
from dedupe.core.models import CancelToken, FileEntry, ScanOptions
from dedupe.core.scanner import scan_files
from tests.core.imagegen import photo, save

pytest.importorskip("imagehash")

OLD_SCHEMA = """
CREATE TABLE hashes (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    inode INTEGER NOT NULL, device INTEGER NOT NULL, partial TEXT, full TEXT
)
"""


@pytest.fixture
def cache() -> Iterator[HashCache]:
    c = HashCache()
    yield c
    c.close()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every file that is really decoded and hashed (cache hits do not appear)."""
    seen: list[Path] = []
    real = perceptual.perceptual_hash

    def counting(path: Path) -> perceptual.PerceptualHash:
        seen.append(path)
        return real(path)

    monkeypatch.setattr(perceptual, "perceptual_hash", counting)
    return seen


def entries_of(root: Path) -> list[FileEntry]:
    return scan_files(root, ScanOptions(exclude=()), CancelToken())[0]


def hash_all(root: Path, cache: HashCache | None) -> dict[Path, perceptual.PerceptualHash]:
    out = HashService(CancelToken(), store=cache).perceptual(entries_of(root))
    if cache is not None:
        cache.flush()
    return out


@pytest.fixture
def images(tmp_path: Path) -> Path:
    root = tmp_path / "pics"
    save(photo(1), root / "a.png")
    save(photo(2), root / "b.png")
    return root


def test_existing_database_migrates_in_place() -> None:
    db = paths.hash_db_file()
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(OLD_SCHEMA)
    conn.execute("INSERT INTO hashes VALUES ('/x', 5, 6, 7, 8, 'p', 'f')")
    conn.commit()
    conn.close()
    cache = HashCache()
    try:
        assert cache.enabled, cache.warnings
        assert cache.count() == 1  # the old row survived
        entry = FileEntry(Path("/x"), 5, 6, 7, 8)
        cache.put_perceptual(entry, "v", "stamp")
        cache.flush()
        hit = cache.get(entry)
        assert hit is not None
        assert (hit.partial, hit.full, hit.perceptual, hit.perceptual_algo) == (
            "p",
            "f",
            "v",
            "stamp",
        )
    finally:
        cache.close()


def test_migration_is_idempotent(cache: HashCache) -> None:
    again = HashCache()  # opens the already-migrated database
    try:
        assert again.enabled and not again.warnings
    finally:
        again.close()


def test_second_run_decodes_nothing(images: Path, cache: HashCache, calls: list[Path]) -> None:
    first = hash_all(images, cache)
    assert len(calls) == 2
    calls.clear()
    assert hash_all(images, cache) == first
    assert calls == []


def test_changed_mtime_invalidates_only_that_image(
    images: Path, cache: HashCache, calls: list[Path]
) -> None:
    hash_all(images, cache)
    calls.clear()
    os.utime(images / "a.png", ns=(10**18, 10**18))
    hash_all(images, cache)
    assert calls == [images / "a.png"]


def test_a_different_stamp_invalidates(
    images: Path, cache: HashCache, calls: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    hash_all(images, cache)
    calls.clear()
    monkeypatch.setattr(perceptual, "cache_stamp", lambda: "2:other-imagehash:other-pillow")
    hash_all(images, cache)
    assert len(calls) == 2


@pytest.mark.parametrize("which", [0, 1, 2])
def test_each_part_of_the_stamp_is_checked(
    which: int,
    images: Path,
    cache: HashCache,
    calls: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hash_all(images, cache)
    calls.clear()
    algo, ih, pil = perceptual.cache_stamp().split(":")
    changed = [algo, ih, pil]
    changed[which] += "x"
    monkeypatch.setattr(perceptual, "cache_stamp", lambda: ":".join(changed))
    hash_all(images, cache)
    assert len(calls) == 2


def test_flat_image_is_cached_as_unusable(
    tmp_path: Path, cache: HashCache, calls: list[Path]
) -> None:
    from PIL import Image

    root = tmp_path / "flat"
    save(Image.new("RGB", (64, 64), "gray"), root / "f.png")
    assert not hash_all(root, cache)[root / "f.png"].usable
    calls.clear()
    assert not hash_all(root, cache)[root / "f.png"].usable
    assert calls == []


def test_putting_a_partial_hash_keeps_the_perceptual_hash(images: Path, cache: HashCache) -> None:
    hash_all(images, cache)
    entry = entries_of(images)[0]
    cache.put_partial(entry, "p")
    cache.flush()
    hit = cache.get(entry)
    assert hit is not None and hit.perceptual is not None and hit.partial == "p"


def test_undecodable_image_is_reported_not_cached(tmp_path: Path, cache: HashCache) -> None:
    root = tmp_path / "bad"
    root.mkdir()
    (root / "x.png").write_bytes(b"not an image")
    svc = HashService(CancelToken(), store=cache)
    assert svc.perceptual(entries_of(root)) == {}
    assert [f.path for f in svc.failed] == [str(root / "x.png")]
    assert "unsupported" in svc.failed[0].reason


def test_broken_database_degrades_and_hashing_still_works(images: Path) -> None:
    db = paths.hash_db_file()
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database" * 100)
    cache = HashCache()
    assert not cache.enabled and "unavailable" in cache.warnings[0]
    assert len(hash_all(images, cache)) == 2
    cache.close()
