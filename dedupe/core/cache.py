"""SQLite hash cache keyed on (path, size, mtime_ns, inode, device).

A cached hash is reused only when every key field matches. Writes go through a single
writer thread that commits in batches. Any database problem (corrupt file, locked file)
degrades to "no cache" with a warning; it never fails a scan.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

from dedupe.core import paths
from dedupe.core.hasher import CachedHashes
from dedupe.core.models import FileEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    device INTEGER NOT NULL,
    partial TEXT,
    full TEXT,
    perceptual TEXT,
    perceptual_algo TEXT
)
"""

# Columns added after the first release; an existing database gets them with ALTER TABLE.
MIGRATIONS = (
    ("perceptual", "ALTER TABLE hashes ADD COLUMN perceptual TEXT"),
    ("perceptual_algo", "ALTER TABLE hashes ADD COLUMN perceptual_algo TEXT"),
)

UPSERT = """
INSERT INTO hashes (path, size, mtime_ns, inode, device, partial, full, perceptual, perceptual_algo)
VALUES (:path, :size, :mtime_ns, :inode, :device, :partial, :full, :perceptual, :perceptual_algo)
ON CONFLICT(path) DO UPDATE SET
    partial = CASE WHEN size = excluded.size AND mtime_ns = excluded.mtime_ns
                    AND inode = excluded.inode AND device = excluded.device
                   THEN COALESCE(excluded.partial, partial) ELSE excluded.partial END,
    full = CASE WHEN size = excluded.size AND mtime_ns = excluded.mtime_ns
                 AND inode = excluded.inode AND device = excluded.device
                THEN COALESCE(excluded.full, full) ELSE excluded.full END,
    perceptual = CASE WHEN size = excluded.size AND mtime_ns = excluded.mtime_ns
                       AND inode = excluded.inode AND device = excluded.device
                      THEN COALESCE(excluded.perceptual, perceptual)
                      ELSE excluded.perceptual END,
    perceptual_algo = CASE WHEN size = excluded.size AND mtime_ns = excluded.mtime_ns
                            AND inode = excluded.inode AND device = excluded.device
                           THEN COALESCE(excluded.perceptual_algo, perceptual_algo)
                           ELSE excluded.perceptual_algo END,
    size = excluded.size, mtime_ns = excluded.mtime_ns,
    inode = excluded.inode, device = excluded.device
"""

BATCH = 500
LOOKUP_CHUNK = 500  # stays under SQLite's bound-variable limit


class HashCache:
    def __init__(self, db_path: Path | None = None, timeout: float = 2.0) -> None:
        self.db_path = db_path or paths.hash_db_file()
        self.timeout = timeout
        self.warnings: list[str] = []
        self.enabled = True
        self._local = threading.local()
        self._queue: queue.Queue[dict[str, object] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._writer: threading.Thread | None = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(SCHEMA)
            self._migrate(conn)
            conn.commit()
            conn.close()
        except (sqlite3.Error, OSError) as e:
            self._disable(f"hash cache unavailable ({e}); continuing without it")
            return
        self._writer = threading.Thread(target=self._write_loop, name="hash-cache", daemon=True)
        self._writer.start()

    # -- public API (HashStore) ----------------------------------------------------------

    def get(self, entry: FileEntry) -> CachedHashes | None:
        return self.get_many([entry]).get(entry.path)

    def get_many(self, entries: Sequence[FileEntry]) -> dict[Path, CachedHashes]:
        """Cached hashes for entries whose full key (path, size, mtime, inode, device) matches."""
        if not self.enabled or not entries:
            return {}
        by_path = {str(e.path): e for e in entries}
        keys = list(by_path)
        found: dict[Path, CachedHashes] = {}
        try:
            conn = self._reader()
            for i in range(0, len(keys), LOOKUP_CHUNK):
                chunk = keys[i : i + LOOKUP_CHUNK]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT path, size, mtime_ns, inode, device, partial, full, "
                    "perceptual, perceptual_algo "
                    f"FROM hashes WHERE path IN ({marks})",
                    chunk,
                )
                for path, size, mtime_ns, inode, device, partial, full, perc, algo in rows:
                    e = by_path[path]
                    if (e.size, e.mtime_ns, e.inode, e.device) == (size, mtime_ns, inode, device):
                        found[e.path] = CachedHashes(partial, full, perc, algo)
        except sqlite3.Error as e:
            self._disable(f"hash cache read failed ({e}); continuing without it")
            return {}
        return found

    def put_partial(self, entry: FileEntry, value: str) -> None:
        self._put(entry, value, None)

    def put_full(self, entry: FileEntry, value: str) -> None:
        self._put(entry, None, value)

    def put_perceptual(self, entry: FileEntry, value: str, algo: str) -> None:
        """``algo`` is the perceptual cache stamp; a reader only trusts a matching stamp."""
        self._put(entry, None, None, value, algo)

    # -- housekeeping --------------------------------------------------------------------

    def flush(self) -> None:
        """Block until every queued write has been committed."""
        if self._writer is not None:
            self._queue.join()

    def close(self) -> None:
        if self._writer is not None:
            self._queue.put(None)
            self._writer.join(timeout=5)
            self._writer = None

    def clear(self) -> int:
        """Remove every cached hash. Returns the number of rows removed."""
        self.flush()
        conn = self._connect()
        try:
            n = conn.execute("DELETE FROM hashes").rowcount
            conn.commit()
            conn.execute("VACUUM")
            return n
        finally:
            conn.close()

    def count(self) -> int:
        self.flush()
        row = self._reader().execute("SELECT COUNT(*) FROM hashes").fetchone()
        return int(row[0])

    # -- internals -----------------------------------------------------------------------

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        have = {row[1] for row in conn.execute("PRAGMA table_info(hashes)")}
        for column, ddl in MIGRATIONS:
            if column not in have:
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):  # another process migrated first
                        raise

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=self.timeout)

    def _reader(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
        return conn

    def _put(
        self,
        entry: FileEntry,
        partial: str | None,
        full: str | None,
        perceptual: str | None = None,
        perceptual_algo: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        self._queue.put(
            {
                "path": str(entry.path),
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "inode": entry.inode,
                "device": entry.device,
                "partial": partial,
                "full": full,
                "perceptual": perceptual,
                "perceptual_algo": perceptual_algo,
            }
        )

    def _write_loop(self) -> None:
        conn = self._connect()
        try:
            stop = False
            while not stop:
                item = self._queue.get()
                batch = []
                taken = 1
                if item is None:
                    stop = True
                else:
                    batch.append(item)
                while len(batch) < BATCH and not stop:
                    try:
                        nxt = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    taken += 1
                    if nxt is None:
                        stop = True
                    else:
                        batch.append(nxt)
                try:
                    if batch and self.enabled:
                        conn.executemany(UPSERT, batch)
                        conn.commit()
                except sqlite3.Error as e:
                    self._disable(f"hash cache write failed ({e}); continuing without it")
                finally:
                    for _ in range(taken):
                        self._queue.task_done()
        finally:
            conn.close()

    def _disable(self, message: str) -> None:
        with self._lock:
            if self.enabled:
                self.enabled = False
                self.warnings.append(message)
