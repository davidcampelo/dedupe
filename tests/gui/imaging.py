from __future__ import annotations

import os
from pathlib import Path

from PIL import Image


def write_image(
    path: Path,
    size: tuple[int, int] = (64, 32),
    color: tuple[int, int, int] = (200, 30, 30),
    orientation: int | None = None,
    taken: str | None = None,
    fmt: str | None = None,
) -> Path:
    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    if taken:
        exif[306] = taken
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"exif": exif} if (orientation or taken) else {}
    img.save(path, format=fmt, **kwargs)
    return path


def write_noise_image(path: Path, size: tuple[int, int], seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(size[0] * size[1] * 3)
    Image.frombytes("RGB", size, data).save(path, quality=90)
    return path
