"""SQLite cache of Plex metadata.

The raw Plex JSON is stored and parsed on read, so improving the parser never
requires a rescan.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from assay.models import MediaItem
from assay.plex.parse import display_title, parse_metadata

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    rating_key  TEXT PRIMARY KEY,
    library     TEXT NOT NULL,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    updated_at  INTEGER,
    scanned_at  INTEGER NOT NULL,
    metadata    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_library ON items(library);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def upsert(self, library: str, metadata: dict) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(metadata["ratingKey"]),
                library,
                metadata.get("type", "unknown"),
                display_title(metadata),
                metadata.get("updatedAt"),
                int(time.time()),
                json.dumps(metadata),
            ),
        )

    def commit(self) -> None:
        self._db.commit()

    def updated_at_by_key(self, library: str) -> dict[str, int | None]:
        rows = self._db.execute("SELECT rating_key, updated_at FROM items WHERE library = ?", (library,))
        return dict(rows.fetchall())

    def delete(self, rating_keys: Iterable[str]) -> None:
        self._db.executemany("DELETE FROM items WHERE rating_key = ?", ((k,) for k in rating_keys))

    def get(self, rating_key: str) -> MediaItem | None:
        row = self._db.execute("SELECT metadata FROM items WHERE rating_key = ?", (rating_key,)).fetchone()
        return parse_metadata(json.loads(row[0])) if row else None

    def search(self, text: str, limit: int = 20) -> list[MediaItem]:
        rows = self._db.execute(
            "SELECT metadata FROM items WHERE title LIKE ? ORDER BY title LIMIT ?",
            (f"%{text}%", limit),
        )
        return [parse_metadata(json.loads(r[0])) for r in rows]

    def items(self, library: str | None = None) -> Iterator[MediaItem]:
        if library:
            rows = self._db.execute("SELECT metadata FROM items WHERE library = ? ORDER BY title", (library,))
        else:
            rows = self._db.execute("SELECT metadata FROM items ORDER BY title")
        for (metadata,) in rows:
            yield parse_metadata(json.loads(metadata))

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
