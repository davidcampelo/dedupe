"""Thumbnails and image metadata, decoded off the GUI thread.

``ThumbnailService.request`` returns a cached result at once or starts a ``Job`` on a small
dedicated pool and emits ``ready`` when it finishes. Decoding uses Pillow (EXIF orientation
applied, HEIC when pillow-heif is installed). Results are cached in a memory LRU, on disk under
``$XDG_CACHE_HOME/dedupe/thumbs/`` keyed by content hash, and the freedesktop thumbnail cache
(``~/.cache/thumbnails``) is reused when its entry is still valid.

This module is on the GUI-thread-rules allowlist: the decode functions run in jobs.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PIL import Image
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from dedupe.core import paths
from dedupe.core.imaging import load_image, safe_exif
from dedupe.core.models import CancelToken, ProgressCallback
from dedupe.gui.workers import Job, JobRunner

THUMB_SIZE = 192
PREVIEW_SIZE = 1600
MEMORY_ITEMS = 256
EXIF_DATE_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime


@dataclass(frozen=True, slots=True)
class ImageInfo:
    width: int = 0
    height: int = 0
    exif_date: str = ""


@dataclass(frozen=True, slots=True)
class ThumbResult:
    key: str
    image: QImage | None
    info: ImageInfo
    error: str = ""
    source: str = "decoded"  # "decoded", "disk cache" or "freedesktop cache"


def cache_key(path: Path, size: int, mtime_ns: int) -> str:
    return f"{path}|{size}|{mtime_ns}"


# -- decoding (runs in worker threads) ------------------------------------------------------


def _read_info(img: Image.Image) -> ImageInfo:
    width, height = img.size
    exif = safe_exif(img)
    if exif.get(274) in (5, 6, 7, 8):  # rotated by EXIF: report the displayed size
        width, height = height, width
    return _info_from_exif(width, height, exif)


def _info_from_exif(width: int, height: int, exif: Image.Exif) -> ImageInfo:
    date = ""
    try:
        sub = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
        for tag in EXIF_DATE_TAGS:
            value = sub.get(tag) or exif.get(tag)
            if value:
                date = _format_exif_date(str(value))
                break
    except Exception:  # damaged EXIF must never break thumbnails
        date = ""
    return ImageInfo(width, height, date)


def _format_exif_date(raw: str) -> str:
    try:
        return datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw.strip()


def _to_qimage(img: Image.Image) -> QImage:
    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    return QImage(
        data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888
    ).copy()


def decode_image(path: Path, size: int) -> tuple[Image.Image, ImageInfo]:
    """Open, orient and shrink an image. Raises on corrupt or unsupported files."""
    loaded = load_image(path, size)
    return loaded.image, _info_from_exif(loaded.width, loaded.height, loaded.exif)


def freedesktop_path(path: Path, size: int) -> Path:
    """Where the freedesktop thumbnail for ``path`` would live (normal 128 px / large 256 px)."""
    uri = "file://" + quote(str(path))
    digest = hashlib.md5(uri.encode(), usedforsecurity=False).hexdigest()
    folder = "large" if size > 128 else "normal"
    return Path.home() / ".cache" / "thumbnails" / folder / f"{digest}.png"


def read_freedesktop(path: Path, size: int, mtime_ns: int) -> Image.Image | None:
    thumb = freedesktop_path(path, size)
    try:
        with Image.open(thumb) as img:
            if str(img.info.get("Thumb::MTime", "")) != str(mtime_ns // 1_000_000_000):
                return None  # stale: the file changed after the thumbnail was made
            img.load()
            return img.copy()
    except (OSError, ValueError):
        return None


def disk_cache_path(content_hash: str, size: int) -> Path:
    return paths.thumbs_dir() / f"{content_hash[:32]}-{size}.png"


def make_thumbnail(
    path: Path,
    size: int,
    mtime_ns: int,
    content_hash: str,
    key: str,
    cancel: CancelToken,
) -> ThumbResult:
    """Produce a thumbnail, trying the caches first. Never raises: errors become a result."""
    try:
        cancel.raise_if_cancelled()
        disk = disk_cache_path(content_hash, size) if content_hash else None
        if disk is not None and disk.exists():
            try:
                with Image.open(disk) as cached:
                    cached.load()
                    img = cached.copy()
                info = _info_from_disk(img)
                return ThumbResult(key, _to_qimage(img), info, source="disk cache")
            except (OSError, ValueError):
                pass  # unreadable cache entry: decode again
        fd = read_freedesktop(path, size, mtime_ns) if size <= 256 else None
        if fd is not None:
            with Image.open(path) as original:  # resolution and EXIF still come from the file
                info = _read_info(original)
            return ThumbResult(key, _to_qimage(fd), info, source="freedesktop cache")
        cancel.raise_if_cancelled()
        img, info = decode_image(path, size)
        if disk is not None:
            _write_disk_cache(disk, img, info)
        return ThumbResult(key, _to_qimage(img), info)
    except Exception as e:
        return ThumbResult(key, None, ImageInfo(), error=f"{type(e).__name__}: {e}")


def _write_disk_cache(target: Path, img: Image.Image, info: ImageInfo) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        from PIL.PngImagePlugin import PngInfo

        meta = PngInfo()
        meta.add_text("dedupe:width", str(info.width))
        meta.add_text("dedupe:height", str(info.height))
        meta.add_text("dedupe:exif", info.exif_date)
        tmp = target.with_name(target.name + f".{threading.get_ident()}.tmp")
        img.save(tmp, format="PNG", pnginfo=meta)
        tmp.replace(target)
    except OSError:
        pass  # the cache is an optimisation only


def _info_from_disk(img: Image.Image) -> ImageInfo:
    meta = img.info
    try:
        return ImageInfo(
            int(meta.get("dedupe:width", img.width)),
            int(meta.get("dedupe:height", img.height)),
            str(meta.get("dedupe:exif", "")),
        )
    except ValueError:
        return ImageInfo(img.width, img.height)


# -- the service (GUI-thread object) --------------------------------------------------------


class ThumbnailService(QObject):
    ready = Signal(object)  # ThumbResult

    def __init__(self, parent: QObject | None = None, threads: int = 2) -> None:
        super().__init__(parent)
        self._runner = JobRunner(threads)
        self._memory: OrderedDict[str, ThumbResult] = OrderedDict()
        self._pending: dict[str, Job] = {}

    def request(
        self,
        path: Path,
        size: int,
        mtime_ns: int,
        content_hash: str = "",
    ) -> ThumbResult | None:
        """The thumbnail if it is already in memory, else None; ``ready`` fires when it arrives."""
        key = cache_key(path, size, mtime_ns)
        hit = self._memory.get(key)
        if hit is not None:
            self._memory.move_to_end(key)
            return hit
        if key in self._pending:
            return None

        def work(cancel: CancelToken, progress: ProgressCallback) -> ThumbResult:
            return make_thumbnail(path, size, mtime_ns, content_hash, key, cancel)

        job = Job(work)
        job.signals.finished.connect(self._on_finished)
        self._pending[key] = job
        self._runner.start(job, -1)  # lowest priority: never compete with scans for CPU
        return None

    def cancel_pending(self, keep: set[str]) -> int:
        """Cancel queued/running jobs for thumbnails that are no longer wanted."""
        cancelled = 0
        for key, job in list(self._pending.items()):
            if key not in keep:
                job.cancel()
                cancelled += 1
        return cancelled

    def pending_count(self) -> int:
        return len(self._pending)

    def clear_memory(self) -> None:
        self._memory.clear()

    def shutdown(self, timeout_ms: int = 2000) -> None:
        self._runner.shutdown(timeout_ms)

    def _on_finished(self, result: ThumbResult) -> None:
        self._pending.pop(result.key, None)
        if result.error and "CancelledError" in result.error:
            return  # cancelled before it started: nobody is waiting for it
        self._memory[result.key] = result
        while len(self._memory) > MEMORY_ITEMS:
            self._memory.popitem(last=False)
        self.ready.emit(result)
