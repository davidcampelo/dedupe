"""Synthetic "photos" for the similar-image tests: smooth random colour fields, which behave
like real photographs under resizing and JPEG compression (unlike flat colours or white noise)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def photo(seed: int, size: tuple[int, int] = (320, 240)) -> Image.Image:
    rng = np.random.default_rng(seed)
    coarse = rng.integers(0, 256, (9, 12, 3), dtype=np.uint8)
    return Image.fromarray(coarse).resize(size, Image.Resampling.BICUBIC)


def save(img: Image.Image, path: Path, **kwargs: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, **kwargs)  # type: ignore[arg-type]
    return path


def letterboxed(img: Image.Image, pad: int = 40) -> Image.Image:
    canvas = Image.new("RGB", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0))
    canvas.paste(img, (pad, pad))
    return canvas


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()
