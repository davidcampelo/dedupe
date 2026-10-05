"""File-type categories for the duplicates filter (by extension only; no disk access)."""

from __future__ import annotations

from pathlib import PurePath

IMAGES = "Images"
VIDEO = "Video"
AUDIO = "Audio"
DOCUMENTS = "Documents"
ARCHIVES = "Archives"
OTHER = "Other"

CATEGORIES = (IMAGES, VIDEO, AUDIO, DOCUMENTS, ARCHIVES, OTHER)

_EXTENSIONS: dict[str, frozenset[str]] = {
    IMAGES: frozenset(
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
    ),
    VIDEO: frozenset(
        {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp"}
    ),
    AUDIO: frozenset(
        {".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".wma", ".aiff"}
    ),
    DOCUMENTS: frozenset(
        {
            ".pdf",
            ".doc",
            ".docx",
            ".odt",
            ".rtf",
            ".txt",
            ".md",
            ".xls",
            ".xlsx",
            ".ods",
            ".csv",
            ".ppt",
            ".pptx",
            ".odp",
            ".epub",
            ".tex",
        }
    ),
    ARCHIVES: frozenset(
        {
            ".zip",
            ".tar",
            ".gz",
            ".tgz",
            ".bz2",
            ".xz",
            ".7z",
            ".rar",
            ".zst",
            ".iso",
            ".deb",
            ".rpm",
        }
    ),
}
_BY_EXTENSION = {ext: cat for cat, exts in _EXTENSIONS.items() for ext in exts}

IMAGE_EXTENSIONS = _EXTENSIONS[IMAGES]


def classify(path: PurePath | str) -> str:
    return _BY_EXTENSION.get(PurePath(path).suffix.lower(), OTHER)


def is_image(path: PurePath | str) -> bool:
    return classify(path) == IMAGES
