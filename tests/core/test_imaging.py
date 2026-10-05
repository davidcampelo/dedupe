from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from dedupe.core import imaging
from dedupe.core.imaging import ImageLoadError, heif_available, load_image, similarity_extensions


def _save(path: Path, size: tuple[int, int] = (40, 20), **kwargs: object) -> Path:
    img = Image.new("RGB", size, (200, 30, 30))
    img.putpixel((0, 0), (0, 0, 255))  # marks the top-left corner
    img.save(path, **kwargs)  # type: ignore[arg-type]
    return path


def test_plain_image_loads_with_its_size(tmp_path: Path) -> None:
    loaded = load_image(_save(tmp_path / "a.png"))
    assert loaded.image.size == (40, 20)
    assert (loaded.width, loaded.height) == (40, 20)


def test_exif_rotation_is_applied(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 clockwise to display
    loaded = load_image(_save(tmp_path / "r.jpg", exif=exif))
    assert loaded.image.size == (20, 40)
    assert (loaded.width, loaded.height) == (20, 40)


def test_max_size_shrinks_the_image_but_not_the_reported_size(tmp_path: Path) -> None:
    loaded = load_image(_save(tmp_path / "big.png", (400, 200)), max_size=50)
    assert max(loaded.image.size) == 50
    assert (loaded.width, loaded.height) == (400, 200)


def test_animated_image_uses_the_first_frame(tmp_path: Path) -> None:
    frames = [Image.new("RGB", (30, 30), c) for c in ((255, 0, 0), (0, 255, 0))]
    gif = tmp_path / "anim.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:])
    loaded = load_image(gif)
    assert loaded.image.convert("RGB").getpixel((5, 5))[0] > 200


def test_corrupt_file_is_an_image_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"this is not an image at all")
    with pytest.raises(ImageLoadError, match="unsupported"):
        load_image(bad)


def test_truncated_file_is_an_image_load_error(tmp_path: Path) -> None:
    good = tmp_path / "t.png"
    img = Image.effect_noise((200, 200), 80).convert("RGB")
    img.save(good)
    data = good.read_bytes()
    good.write_bytes(data[: len(data) // 2])
    with pytest.raises(ImageLoadError, match="corrupt or truncated"):
        load_image(good)


def test_decompression_bomb_is_an_image_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _save(tmp_path / "bomb.png", (100, 100))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)  # 100x100 > 2 * 10
    with pytest.raises(ImageLoadError, match="decompression bomb"):
        load_image(path)


def test_missing_file_keeps_its_io_error_type(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_image(tmp_path / "gone.png")


@pytest.mark.skipif(not heif_available(), reason="pillow-heif is not installed")
def test_heif_loads_when_pillow_heif_is_installed(tmp_path: Path) -> None:
    path = tmp_path / "x.heic"
    Image.new("RGB", (32, 24), (10, 200, 10)).save(path, format="HEIF")
    assert load_image(path).image.size == (32, 24)


def test_similarity_extensions_exclude_vector_icon_and_raw() -> None:
    exts = similarity_extensions()
    assert {".jpg", ".png", ".webp"} <= exts
    assert not exts & {".svg", ".ico", ".raw", ".cr2", ".nef", ".arw", ".dng"}


def test_heic_is_listed_only_when_it_can_be_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imaging, "_HEIF", False)
    assert ".heic" not in similarity_extensions()
    monkeypatch.setattr(imaging, "_HEIF", True)
    assert ".heic" in similarity_extensions()
