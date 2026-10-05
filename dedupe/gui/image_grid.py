"""Grid-of-thumbnails browse mode: one tile per image group.

Thumbnails are requested lazily, only for tiles the view actually paints, and requests for tiles
that scrolled away are cancelled. A placeholder is shown until the image arrives."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QWidget

from dedupe.core.formatting import human
from dedupe.core.models import DuplicateGroup, FileEntry
from dedupe.gui.file_types import is_image
from dedupe.gui.thumbnails import THUMB_SIZE, ThumbnailService, ThumbResult, cache_key

Index = QModelIndex | QPersistentModelIndex
GROUP_ROLE = Qt.ItemDataRole.UserRole + 2
PIXMAP_CACHE = 400
TILE = QSize(THUMB_SIZE + 24, THUMB_SIZE + 62)


def image_groups(groups: Sequence[DuplicateGroup]) -> list[DuplicateGroup]:
    """Groups that contain at least one image (computed in a job for large lists)."""
    return [g for g in groups if any(is_image(f.path) for f in g.files)]


def representative(group: DuplicateGroup) -> FileEntry:
    return next((f for f in group.files if is_image(f.path)), group.files[0])


def _placeholder() -> QIcon:
    pm = QPixmap(THUMB_SIZE, THUMB_SIZE)
    pm.fill(QColor(128, 128, 128, 60))
    return QIcon(pm)


class ImageGridModel(QAbstractListModel):
    def __init__(self, service: ThumbnailService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.groups: list[DuplicateGroup] = []
        self._key_row: dict[str, int] = {}
        self._pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._failed: set[str] = set()
        self._placeholder = _placeholder()
        service.ready.connect(self._on_ready)

    def set_groups(self, groups: Sequence[DuplicateGroup]) -> None:
        self.beginResetModel()
        self.groups = list(groups)
        self._key_row = {}
        for row, g in enumerate(self.groups):
            rep = representative(g)
            self._key_row[cache_key(rep.path, THUMB_SIZE, rep.mtime_ns)] = row
        self._failed = set()
        self.endResetModel()

    def row_of(self, group: DuplicateGroup | None) -> int:
        if group is None:
            return -1
        for row, g in enumerate(self.groups):
            if g is group:
                return row
        return -1

    def visible_keys(self, rows: range) -> set[str]:
        keys = set()
        for row in rows:
            if 0 <= row < len(self.groups):
                rep = representative(self.groups[row])
                keys.add(cache_key(rep.path, THUMB_SIZE, rep.mtime_ns))
        return keys

    # -- QAbstractListModel --------------------------------------------------------------

    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.groups)

    def flags(self, index: Index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.groups):
            return None
        group = self.groups[index.row()]
        if role == GROUP_ROLE:
            return group
        if role == Qt.ItemDataRole.DisplayRole:
            rep = representative(group)
            return f"{rep.path.name}\n{len(group.files)} copies · {human(group.size)}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(str(f.path) for f in group.files[:10])
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(group)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        return None

    def _thumbnail(self, group: DuplicateGroup) -> QIcon:
        rep = representative(group)
        key = cache_key(rep.path, THUMB_SIZE, rep.mtime_ns)
        pm = self._pixmaps.get(key)
        if pm is not None:
            self._pixmaps.move_to_end(key)
            return QIcon(pm)
        if key in self._failed:
            return self._placeholder
        hit = self.service.request(rep.path, THUMB_SIZE, rep.mtime_ns, group.hash)  # lazily
        if hit is not None:
            self._store(hit)
            pm = self._pixmaps.get(key)
            if pm is not None:
                return QIcon(pm)
        return self._placeholder

    def _store(self, result: ThumbResult) -> None:
        if result.image is None:
            self._failed.add(result.key)
            return
        self._pixmaps[result.key] = QPixmap.fromImage(result.image)
        while len(self._pixmaps) > PIXMAP_CACHE:
            self._pixmaps.popitem(last=False)

    def _on_ready(self, result: ThumbResult) -> None:
        row = self._key_row.get(result.key)
        if row is None:
            return
        self._store(result)
        index = self.index(row)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])


class ImageGridView(QListView):
    group_selected = Signal(object)  # DuplicateGroup | None

    PRUNE_MS = 150

    def __init__(self, service: ThumbnailService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.grid_model = ImageGridModel(service, self)
        self.setModel(self.grid_model)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(200)
        self.setWrapping(True)
        self.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.setGridSize(TILE)
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._prune_timer = QTimer(self)
        self._prune_timer.setSingleShot(True)
        self._prune_timer.setInterval(self.PRUNE_MS)
        self._prune_timer.timeout.connect(self.prune_offscreen)
        self.verticalScrollBar().valueChanged.connect(lambda _: self._prune_timer.start())

    def set_groups(self, groups: Sequence[DuplicateGroup]) -> None:
        self.grid_model.set_groups(groups)

    def selected_group(self) -> DuplicateGroup | None:
        index = self.currentIndex()
        return self.grid_model.data(index, GROUP_ROLE) if index.isValid() else None

    def select_group(self, group: DuplicateGroup | None) -> bool:
        row = self.grid_model.row_of(group)
        if row < 0:
            return False
        index = self.grid_model.index(row)
        self.setCurrentIndex(index)
        self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def currentChanged(self, current: Index, previous: Index) -> None:
        super().currentChanged(current, previous)
        self.group_selected.emit(
            self.grid_model.data(current, GROUP_ROLE) if current.isValid() else None
        )

    def visible_rows(self) -> range:
        if self.grid_model.rowCount() == 0:
            return range(0)
        vp = self.viewport().rect()
        first = self.indexAt(QPoint(vp.left() + 4, vp.top() + 4))
        last = self.indexAt(QPoint(vp.right() - 4, vp.bottom() - 4))
        start = first.row() if first.isValid() else 0
        # a short last row leaves the corner empty: fall back to the row count that fits
        if last.isValid():
            stop = last.row() + 1
        else:
            per_row = max(1, vp.width() // TILE.width())
            rows_fit = vp.height() // TILE.height() + 2
            stop = min(self.grid_model.rowCount(), start + per_row * rows_fit)
        return range(start, stop)

    def prune_offscreen(self) -> int:
        """Cancel thumbnail jobs for tiles that are no longer on screen."""
        keep = self.grid_model.visible_keys(self.visible_rows())
        return self.service.cancel_pending(keep)
