from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import PIL
import pytest
from PIL import Image, ImageDraw, ImageEnhance

from dedupe.core import perceptual
from dedupe.core.imaging import ImageLoadError
from dedupe.core.perceptual import PerceptualHash, perceptual_hash
from tests.core.imagegen import distance, letterboxed, photo, save, taken_at

pytest.importorskip("imagehash")


def best_distance(a: PerceptualHash, b: PerceptualHash) -> int:
    """Smallest joint distance of a's variants against b's plain hash."""
    return min(max(distance(p, b.phash), distance(d, b.dhash)) for p, d in a.variants)


@pytest.fixture
def original(tmp_path: Path) -> tuple[Image.Image, PerceptualHash]:
    img = photo(1)
    return img, perceptual_hash(save(img, tmp_path / "original.png"))


def hash_of(img: Image.Image, tmp_path: Path, name: str, **kwargs: object) -> PerceptualHash:
    return perceptual_hash(save(img, tmp_path / name, **kwargs))


def test_same_image_has_distance_zero(tmp_path: Path, original: Any) -> None:
    img, h = original
    assert best_distance(h, hash_of(img, tmp_path, "copy.png")) == 0


def test_resized_copy_is_within_8_bits(tmp_path: Path, original: Any) -> None:
    img, h = original
    small = img.resize((160, 120), Image.Resampling.LANCZOS)
    assert best_distance(h, hash_of(small, tmp_path, "small.png")) <= 8


def test_low_quality_jpeg_is_within_8_bits(tmp_path: Path, original: Any) -> None:
    img, h = original
    assert best_distance(h, hash_of(img, tmp_path, "q60.jpg", quality=60)) <= 8


def test_brighter_copy_is_within_8_bits(tmp_path: Path, original: Any) -> None:
    img, h = original
    bright = ImageEnhance.Brightness(img).enhance(1.1)
    assert best_distance(h, hash_of(bright, tmp_path, "bright.png")) <= 8


def test_exif_rotated_copy_is_within_8_bits(tmp_path: Path, original: Any) -> None:
    img, h = original
    # pixels stored rotated, tag says "rotate 90 clockwise to display": displays like the original
    exif = Image.Exif()
    exif[274] = 6
    stored = img.transpose(Image.Transpose.ROTATE_90)
    assert best_distance(h, hash_of(stored, tmp_path, "exif.jpg", exif=exif, quality=95)) <= 8


def test_rotated_without_exif_is_found_by_a_variant(tmp_path: Path, original: Any) -> None:
    img, h = original
    rotated = hash_of(img.transpose(Image.Transpose.ROTATE_90), tmp_path, "rot.png")
    assert best_distance(h, rotated) <= 8
    assert distance(h.phash, rotated.phash) > 8  # only the variants make the match


def test_mirrored_copy_is_found_by_a_variant(tmp_path: Path, original: Any) -> None:
    img, h = original
    mirrored = hash_of(img.transpose(Image.Transpose.FLIP_LEFT_RIGHT), tmp_path, "mir.png")
    assert best_distance(h, mirrored) <= 8
    assert distance(h.phash, mirrored.phash) > 8


def test_letterboxed_copy_is_within_8_bits(tmp_path: Path, original: Any) -> None:
    img, h = original
    assert best_distance(h, hash_of(letterboxed(img), tmp_path, "box.png")) <= 8


def test_different_photos_are_far_apart(tmp_path: Path, original: Any) -> None:
    _, h = original
    other = hash_of(photo(2), tmp_path, "other.png")
    assert best_distance(h, other) > 16


def test_gradient_and_checkerboard_are_far_apart(tmp_path: Path) -> None:
    gradient = Image.linear_gradient("L").rotate(90).resize((128, 128)).convert("RGB")
    ImageDraw.Draw(gradient).ellipse((20, 30, 70, 90), fill=(255, 255, 255))  # a pure ramp is flat
    checker = Image.new("RGB", (128, 128), "white")
    draw = ImageDraw.Draw(checker)
    for x in range(0, 128, 16):
        for y in range(0, 128, 16):
            if (x + y) // 16 % 2:
                draw.rectangle((x, y, x + 15, y + 15), fill="black")
    g, c = hash_of(gradient, tmp_path, "g.png"), hash_of(checker, tmp_path, "c.png")
    assert g.usable and c.usable
    assert distance(g.phash, c.phash) > 20


def test_solid_colour_image_is_excluded(tmp_path: Path) -> None:
    h = hash_of(Image.new("RGB", (100, 100), (30, 90, 200)), tmp_path, "solid.png")
    assert not h.usable and h.variants == ()


def test_letterboxed_solid_image_is_excluded(tmp_path: Path) -> None:
    assert not hash_of(letterboxed(Image.new("RGB", (100, 80), "red")), tmp_path, "x.png").usable


def test_transparent_pixels_are_shown_on_white(tmp_path: Path) -> None:
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))  # fully transparent, junk RGB underneath
    ImageDraw.Draw(img).ellipse((32, 32, 96, 96), fill=(255, 0, 0, 255))
    flat = Image.new("RGB", (128, 128), "white")
    ImageDraw.Draw(flat).ellipse((32, 32, 96, 96), fill=(255, 0, 0))
    a, b = hash_of(img, tmp_path, "a.png"), hash_of(flat, tmp_path, "b.png")
    assert best_distance(a, b) <= 4


def test_undecodable_file_raises_image_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"nope")
    with pytest.raises(ImageLoadError):
        perceptual_hash(bad)


def test_encode_decode_roundtrip(tmp_path: Path, original: Any) -> None:
    _, h = original
    assert PerceptualHash.decode(h.encode()) == h
    flat = hash_of(Image.new("RGB", (64, 64), "gray"), tmp_path, "flat.png")
    assert PerceptualHash.decode(flat.encode()) == flat


def test_encode_decode_roundtrip_keeps_the_capture_time(tmp_path: Path) -> None:
    h = hash_of(photo(1), tmp_path, "t.jpg", exif=taken_at("2011:05:01 06:08:58"))
    assert h.taken is not None
    assert PerceptualHash.decode(h.encode()) == h


@pytest.mark.parametrize(
    "text",
    ["", "x", "1,2;abc", "1,2;" + "0" * 32, "a,b;", "1,2,3,4;", "1,2,x;", "1,2;|!!", "1,2;|AAAA"],
)
def test_decode_rejects_malformed_text(text: str) -> None:
    assert PerceptualHash.decode(text) is None


def test_capture_time_comes_from_exif(tmp_path: Path) -> None:
    first = hash_of(photo(1), tmp_path, "a.jpg", exif=taken_at("2011:05:01 06:08:58"))
    later = hash_of(photo(1), tmp_path, "b.jpg", exif=taken_at("2011:05:01 06:09:02"))
    assert first.taken is not None and later.taken is not None
    assert later.taken - first.taken == 4


def test_capture_time_falls_back_to_datetime(tmp_path: Path) -> None:
    exif = taken_at("2011:05:01 06:08:58", original=False)
    assert hash_of(photo(1), tmp_path, "a.jpg", exif=exif).taken is not None


@pytest.mark.parametrize("when", ["0000:00:00 00:00:00", "    :  :     :  :  ", "garbage"])
def test_malformed_capture_time_is_none(tmp_path: Path, when: str) -> None:
    assert hash_of(photo(1), tmp_path, "a.jpg", exif=taken_at(when)).taken is None


def test_image_without_exif_has_no_capture_time_and_no_sketch(original: Any) -> None:
    _, h = original
    assert h.taken is None and h.sketch == b"" and h.sketch_pixels() is None


def test_image_with_a_capture_time_has_a_sketch_of_its_working_copy(tmp_path: Path) -> None:
    path = save(photo(1), tmp_path / "a.jpg", exif=taken_at("2011:05:01 06:08:58"))
    sketch = perceptual.perceptual_hash(path).sketch_pixels()
    work = perceptual.working_copy(path).astype(float)
    assert sketch is not None and sketch.shape == (32, 32)
    halved = work.reshape(32, 2, 32, 2).mean(axis=(1, 3))
    assert abs(sketch.astype(float) - halved).max() <= 0.5


def test_flat_image_with_a_capture_time_has_no_sketch(tmp_path: Path) -> None:
    flat = Image.new("RGB", (64, 64), "gray")
    h = hash_of(flat, tmp_path, "f.jpg", exif=taken_at("2011:05:01 06:08:58"))
    assert not h.usable and h.sketch == b""


def test_cache_stamp_names_the_algorithm_and_both_library_versions() -> None:
    import imagehash

    algo, ih, pil = perceptual.cache_stamp().split(":")
    assert (algo, ih, pil) == (
        str(perceptual.ALGO_VERSION),
        imagehash.__version__,
        PIL.__version__,
    )


def test_golden_values(tmp_path: Path) -> None:
    """Fixed images give fixed hashes. If this fails after upgrading imagehash or Pillow, the
    hash values moved: review that cache entries are invalidated (the stamp carries both
    versions) and update the numbers."""
    h = perceptual_hash(save(photo(7, (200, 150)), tmp_path / "golden.png"))
    assert (h.phash, h.dhash) == GOLDEN


GOLDEN = (11838683335904321060, 2470685877582858796)


def test_without_imagehash_the_feature_reports_unavailable_and_nothing_else_breaks(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("same")
    (tmp_path / "b.txt").write_text("same")
    code = (
        "import sys\n"
        "sys.modules['imagehash'] = None; sys.modules['numpy'] = None\n"
        "from dedupe.core.perceptual import similar_available\n"
        "from dedupe.core.pipeline import run_scan\n"
        "from dedupe.core.models import ScanOptions\n"
        f"r = run_scan(__import__('pathlib').Path({str(tmp_path)!r}), ScanOptions(exclude=()))\n"
        "print(similar_available(), len(r.groups))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["False", "1"]
