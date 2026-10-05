"""Perceptual image hashes (pHash + dHash) for the "similar images" feature.

All use of ``imagehash`` goes through this module, and it is imported lazily, so the rest of the
program (and the app without the ``similar`` extra) never needs it. numpy is imported lazily for
the same reason. ``similar_available()`` says whether the extra is installed.

An image is decoded once, flattened to a grayscale 64x64 *working copy* (uniform borders trimmed)
and hashed in all 8 rotations and mirror images. Two images are compared by the Hamming distance
between one's variant hashes and the other's plain hash (see ``core/similarity.py``), which
catches rotated and mirrored copies. Hashes are plain ``int`` values; ``ImageHash`` objects never
leave this module.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import PIL
from PIL import Image

from dedupe.core.imaging import load_image

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

# Bump when our own steps change (border trim, variants, the uniform filter, the working size).
ALGO_VERSION = 1

WORK_SIZE = 64  # the working copy that is hashed and compared with SSIM
DECODE_SIZE = 256  # images are shrunk to this while decoding (JPEG draft mode makes it cheap)
VARIANTS = 8
BORDER_TOLERANCE = 12  # gray levels a border pixel may differ from its corner and still be border
MIN_TRIMMED = 16  # never trim below this many pixels on a side
UNIFORM_STD = 2.0  # a working copy this flat has no content to compare


def similar_available() -> bool:
    """True when the optional ``similar`` extra (imagehash and numpy) is installed."""
    return (
        importlib.util.find_spec("imagehash") is not None
        and importlib.util.find_spec("numpy") is not None
    )


def cache_stamp() -> str:
    """Identifies the exact hashing code: a change in any part invalidates cached hashes."""
    import imagehash

    return f"{ALGO_VERSION}:{imagehash.__version__}:{PIL.__version__}"


@dataclass(frozen=True, slots=True)
class PerceptualHash:
    phash: int  # 64-bit pHash of the image as is
    dhash: int  # 64-bit dHash of the image as is
    # (phash, dhash) of the 8 rotations/mirror images; ``variants[0] == (phash, dhash)``.
    # Empty when the image is unusable (uniform): it would match everything.
    variants: tuple[tuple[int, int], ...]
    width: int
    height: int

    @property
    def usable(self) -> bool:
        return bool(self.variants)

    def encode(self) -> str:
        """Compact text form for the cache (the 64x64 thumbnail is never stored)."""
        body = ";".join(f"{p:016x}{d:016x}" for p, d in self.variants)
        return f"{self.width},{self.height};{body}"

    @classmethod
    def decode(cls, text: str) -> PerceptualHash | None:
        """Inverse of ``encode``; None for anything malformed (the caller re-hashes)."""
        try:
            head, _, body = text.partition(";")
            w, h = (int(x) for x in head.split(","))
            variants = tuple(
                (int(item[:16], 16), int(item[16:], 16))
                for item in body.split(";")
                if item and len(item) == 32
            )
            if body and len(variants) != VARIANTS:
                return None
        except ValueError:
            return None
        if not variants:
            return cls(0, 0, (), w, h)
        return cls(variants[0][0], variants[0][1], variants, w, h)


# -- the working copy -----------------------------------------------------------------------


def variant(arr: npt.NDArray[np.uint8], k: int) -> npt.NDArray[np.uint8]:
    """The k-th of the 8 rotations/mirror images of a square array (k=0 is the array itself)."""
    import numpy as np

    out = (
        arr,
        np.rot90(arr, 1),
        np.rot90(arr, 2),
        np.rot90(arr, 3),
        arr[:, ::-1],
        arr[::-1, :],
        arr.T,
        arr[::-1, ::-1].T,
    )[k]
    return np.ascontiguousarray(out)


def _flatten_alpha(img: Image.Image) -> Image.Image:
    """Transparent pixels hold arbitrary colour data; show the image on white, as a viewer does."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(base, rgba).convert("L")
    return img.convert("L")


def _trim_border(gray: Image.Image) -> Image.Image:
    """Remove a uniform border (letterbox, padding). Only when all four corners agree."""
    from PIL import ImageChops

    w, h = gray.size
    corners = [int(gray.getpixel(xy)) for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]  # type: ignore[arg-type]
    if max(corners) - min(corners) > BORDER_TOLERANCE:
        return gray
    bg = Image.new("L", gray.size, corners[0])
    diff = ImageChops.difference(gray, bg).point(lambda v: 255 if v > BORDER_TOLERANCE else 0)
    box = diff.getbbox()
    if box is None or box[2] - box[0] < MIN_TRIMMED or box[3] - box[1] < MIN_TRIMMED:
        return gray
    return gray.crop(box)


@dataclass(frozen=True, slots=True)
class Analysis:
    pixels: npt.NDArray[np.uint8]  # WORK_SIZE x WORK_SIZE grayscale working copy
    width: int
    height: int


def analyse(path: Path) -> Analysis:
    """Decode once and build the working copy. Raises ``ImageLoadError`` (an OSError)."""
    import numpy as np

    loaded = load_image(path, DECODE_SIZE)
    gray = _trim_border(_flatten_alpha(loaded.image))
    small = gray.resize((WORK_SIZE, WORK_SIZE), Image.Resampling.LANCZOS)
    return Analysis(np.asarray(small, dtype=np.uint8), loaded.width, loaded.height)


def working_copy(path: Path) -> npt.NDArray[np.uint8]:
    """The 64x64 grayscale image used for the SSIM check."""
    return analyse(path).pixels


# -- hashing --------------------------------------------------------------------------------


def _to_int(bits: Any) -> int:
    import numpy as np

    return int.from_bytes(np.packbits(bits.ravel()).tobytes(), "big")


def hash_pixels(pixels: npt.NDArray[np.uint8]) -> tuple[int, int]:
    """(pHash, dHash) of one square array, as plain ints."""
    import imagehash

    img = Image.fromarray(pixels, "L")
    return _to_int(imagehash.phash(img).hash), _to_int(imagehash.dhash(img).hash)


def hash_analysis(a: Analysis) -> PerceptualHash:
    """All 8 variants of the working copy, or an unusable result for a flat image."""
    import numpy as np

    flat = float(np.std(a.pixels)) < UNIFORM_STD
    if flat:
        return PerceptualHash(0, 0, (), a.width, a.height)
    variants = tuple(hash_pixels(variant(a.pixels, k)) for k in range(VARIANTS))
    p0 = variants[0][0]
    if p0.bit_count() <= 2 or p0.bit_count() >= 62:  # would match nearly everything
        return PerceptualHash(0, 0, (), a.width, a.height)
    return PerceptualHash(p0, variants[0][1], variants, a.width, a.height)


def perceptual_hash(path: Path) -> PerceptualHash:
    """Decode ``path`` once and hash it. Raises ``ImageLoadError`` for undecodable files."""
    return hash_analysis(analyse(path))
