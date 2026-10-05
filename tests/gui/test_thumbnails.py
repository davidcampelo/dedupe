from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from pytestqt.qtbot import QtBot

from dedupe.core import paths
from dedupe.core.models import CancelToken
from dedupe.gui import thumbnails
from dedupe.gui.thumbnails import (
    ThumbnailService,
    ThumbResult,
    cache_key,
    decode_image,
    disk_cache_path,
    freedesktop_path,
    make_thumbnail,
)
from tests.gui.imaging import write_image


def make(
    path: Path, size: int = 128, content: str = "", cancel: CancelToken | None = None
) -> ThumbResult:
    st = path.stat()
    return make_thumbnail(path, size, st.st_mtime_ns, content, "k", cancel or CancelToken())


def test_decode_applies_exif_orientation_and_reports_displayed_size(tmp_path: Path) -> None:
    img = write_image(tmp_path / "rot.jpg", (400, 200), orientation=6, taken="2020:05:17 10:30:00")
    result = make(img)
    assert result.error == "" and result.image is not None
    assert (result.info.width, result.info.height) == (200, 400)  # rotated by EXIF 6
    assert result.image.height() > result.image.width()
    assert result.info.exif_date == "2020-05-17 10:30"


def test_large_images_are_shrunk_to_the_thumbnail_size(tmp_path: Path) -> None:
    result = make(write_image(tmp_path / "big.png", (3000, 1500)), size=192)
    assert result.image is not None and max(result.image.width(), result.image.height()) == 192
    assert (result.info.width, result.info.height) == (3000, 1500)


def test_png_and_gif_and_bmp_decode(tmp_path: Path) -> None:
    for name in ("a.png", "a.gif", "a.bmp", "a.webp", "a.tiff"):
        assert make(write_image(tmp_path / name)).error == "", name


def test_heic_decodes_when_pillow_heif_is_available(tmp_path: Path) -> None:
    pytest.importorskip("pillow_heif")
    path = tmp_path / "photo.heic"
    Image.new("RGB", (80, 40), (10, 200, 10)).save(path, format="HEIF")
    result = make(path)
    assert result.error == "" and (result.info.width, result.info.height) == (80, 40)


def test_corrupt_image_becomes_an_error_result_not_a_crash(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8\xff this is not really a jpeg")
    result = make(bad)
    assert result.image is None and result.error
    missing = make_thumbnail(tmp_path / "gone.png", 128, 0, "", "k", CancelToken())
    assert missing.image is None and "FileNotFoundError" in missing.error


def test_disk_cache_is_written_and_reused_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = write_image(tmp_path / "a.jpg", (500, 300), taken="2021:01:02 03:04:05")
    first = make(img, 192, content="abc123")
    cached = disk_cache_path("abc123", 192)
    assert first.source == "decoded" and cached.exists()
    assert cached.is_relative_to(paths.thumbs_dir())

    def no_decode(*a: object, **k: object) -> None:
        raise AssertionError("decoded again although the disk cache has it")

    monkeypatch.setattr(thumbnails, "decode_image", no_decode)
    second = make(img, 192, content="abc123")
    assert second.source == "disk cache" and second.image is not None
    assert (second.info.width, second.info.height, second.info.exif_date) == (
        500,
        300,
        "2021-01-02 03:04",
    )


def test_unreadable_disk_cache_entry_is_ignored(tmp_path: Path) -> None:
    img = write_image(tmp_path / "a.png")
    cache = disk_cache_path("deadbeef", 128)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"garbage")
    assert make(img, 128, content="deadbeef").source == "decoded"


def write_freedesktop(path: Path, mtime_text: str) -> Path:
    thumb = freedesktop_path(path, 128)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    meta = PngInfo()
    meta.add_text("Thumb::MTime", mtime_text)
    Image.new("RGB", (50, 25), (1, 2, 3)).save(thumb, pnginfo=meta)
    return thumb


def test_freedesktop_path_follows_the_spec(tmp_path: Path) -> None:
    p = tmp_path / "my photo.jpg"
    digest = hashlib.md5(("file://" + quote(str(p))).encode(), usedforsecurity=False).hexdigest()
    assert freedesktop_path(p, 128) == Path.home() / ".cache/thumbnails/normal" / f"{digest}.png"
    assert freedesktop_path(p, 256).parent.name == "large"


def test_valid_freedesktop_thumbnail_is_reused_and_a_stale_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = write_image(tmp_path / "a.png", (300, 200))
    mtime_s = img.stat().st_mtime_ns // 1_000_000_000
    write_freedesktop(img, str(mtime_s))
    real_decode = thumbnails.decode_image
    monkeypatch.setattr(
        thumbnails, "decode_image", lambda *a: (_ for _ in ()).throw(AssertionError("decoded"))
    )
    reused = make(img, 128)
    assert reused.source == "freedesktop cache" and (reused.info.width, reused.info.height) == (
        300,
        200,
    )
    monkeypatch.setattr(thumbnails, "decode_image", real_decode)
    write_freedesktop(img, str(mtime_s - 100))  # the file changed after the thumbnail was made
    assert make(img, 128).source == "decoded"


def test_decode_image_returns_a_copy_that_outlives_the_file(tmp_path: Path) -> None:
    img = write_image(tmp_path / "a.png", (100, 100))
    decoded, info = decode_image(img, 50)
    img.unlink()
    assert decoded.size == (50, 50) and info.width == 100 and decoded.getpixel((1, 1))


def test_cancelled_token_stops_before_decoding(tmp_path: Path) -> None:
    token = CancelToken()
    token.cancel()
    result = make(write_image(tmp_path / "a.png"), cancel=token)
    assert result.image is None and "CancelledError" in result.error


# -- the service ----------------------------------------------------------------------------


def test_service_returns_none_then_emits_then_hits_memory(qtbot: QtBot, tmp_path: Path) -> None:
    img = write_image(tmp_path / "a.png", (300, 200))
    mtime = img.stat().st_mtime_ns
    service = ThumbnailService()
    assert service.request(img, 192, mtime) is None  # placeholder case: nothing yet
    assert service.pending_count() == 1
    with qtbot.waitSignal(service.ready, timeout=10000) as blocker:
        pass
    result: ThumbResult = blocker.args[0]
    assert result.key == cache_key(img, 192, mtime) and result.image is not None
    hit = service.request(img, 192, mtime)
    assert hit is result and service.pending_count() == 0
    service.shutdown()


def test_memory_cache_is_a_bounded_lru(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(thumbnails, "MEMORY_ITEMS", 2)
    service = ThumbnailService()
    imgs = [write_image(tmp_path / f"{i}.png") for i in range(3)]
    for img in imgs:
        with qtbot.waitSignal(service.ready, timeout=10000):
            service.request(img, 64, img.stat().st_mtime_ns)
    assert len(service._memory) == 2
    assert service.request(imgs[0], 64, imgs[0].stat().st_mtime_ns) is None  # evicted
    service.shutdown()


def test_cancel_pending_drops_queued_jobs(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    real = thumbnails.decode_image

    def slow(path: Path, size: int):  # type: ignore[no-untyped-def]
        gate.wait(5)
        return real(path, size)

    monkeypatch.setattr(thumbnails, "decode_image", slow)
    service = ThumbnailService(threads=1)
    a, b, c = (write_image(tmp_path / f"{n}.png") for n in "abc")
    for img in (a, b, c):
        service.request(img, 64, img.stat().st_mtime_ns)
    emitted: list[str] = []
    service.ready.connect(lambda r: emitted.append(Path(r.key.split("|")[0]).name))
    keep = {cache_key(c, 64, c.stat().st_mtime_ns)}
    assert service.cancel_pending(keep) == 2  # a (running) and b (queued)
    gate.set()
    qtbot.waitUntil(lambda: service.pending_count() == 0, timeout=10000)
    qtbot.wait(100)
    assert "b.png" not in emitted and "c.png" in emitted
    service.shutdown()
