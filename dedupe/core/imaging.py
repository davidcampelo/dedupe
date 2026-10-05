"""One Pillow loader shared by the engine (perceptual hashing) and the GUI (thumbnails).

``load_image`` opens a file, uses ``draft()`` for a fast JPEG downscale, applies the EXIF
orientation and returns the first frame of animated images. Anything that cannot be decoded is
reported as ``ImageLoadError`` with a human-readable reason, so one bad image never fails a scan.
HEIC/HEIF is registered when pillow-heif is installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # optional HEIC/HEIF support
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF = True
except ImportError:  # pragma: no cover - depends on the environment
    _HEIF = False

# Every extension the GUI treats as an image (by name only; no disk access).
IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".avif",
        ".svg",
        ".ico",
        ".raw",
        ".cr2",
        ".nef",
        ".arw",
        ".dng",
    }
)
# Pillow cannot decode these: vector, icon and camera-RAW formats.
_NOT_RASTER = frozenset({".svg", ".ico", ".raw", ".cr2", ".nef", ".arw", ".dng"})
_NEEDS_HEIF = frozenset({".heic", ".heif"})


def heif_available() -> bool:
    return _HEIF


def similarity_extensions() -> frozenset[str]:
    """Extensions of the raster images that can be decoded in this environment."""
    exts = IMAGE_EXTENSIONS - _NOT_RASTER
    return exts if _HEIF else exts - _NEEDS_HEIF


class ImageLoadError(OSError):
    """The file is not a decodable image. ``reason`` is shown to the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LoadedImage:
    image: Image.Image  # oriented, fully decoded, at most ``max_size`` on its longest side
    width: int  # displayed size of the original (EXIF orientation applied)
    height: int
    exif: Image.Exif


def safe_exif(img: Image.Image) -> Image.Exif:
    try:
        return img.getexif()
    except Exception:  # damaged EXIF must never make an image undecodable
        return Image.Exif()


def load_image(path: Path, max_size: int | None = None) -> LoadedImage:
    """Open, orient and (optionally) shrink an image. Raises ``ImageLoadError``."""
    try:
        with Image.open(path) as img:
            width, height = img.size
            exif = safe_exif(img)
            if exif.get(274) in (5, 6, 7, 8):  # rotated by EXIF: the displayed size is swapped
                width, height = height, width
            if max_size is not None:
                img.draft("RGB", (max_size * 2, max_size * 2))  # fast JPEG downscale
            oriented = ImageOps.exif_transpose(img)
            if max_size is not None:
                oriented.thumbnail((max_size, max_size))
            oriented.load()  # a truncated file fails here, inside the try
            return LoadedImage(oriented.copy(), width, height, exif)
    except Image.DecompressionBombError as e:
        raise ImageLoadError("image too large (decompression bomb)") from e
    except UnidentifiedImageError as e:
        raise ImageLoadError("unsupported or unrecognised image format") from e
    except (FileNotFoundError, PermissionError):
        raise  # plain I/O problems keep their type; the engine words them as it does elsewhere
    except (OSError, ValueError, SyntaxError, EOFError) as e:
        # Pillow reports corrupt and truncated data as OSError/SyntaxError/ValueError.
        raise ImageLoadError(f"corrupt or truncated image ({e})") from e
