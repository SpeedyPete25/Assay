"""SQLite cache of Plex metadata.

The raw Plex JSON is stored and parsed on read, so improving the parser never
requires a rescan.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from assay.models import Impact, MediaItem
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

-- Real playback decisions from /status/sessions, next to Assay's prediction.
CREATE TABLE IF NOT EXISTS playbacks (
    id                  INTEGER PRIMARY KEY,
    dedupe_key          TEXT NOT NULL UNIQUE,
    observed_at         INTEGER NOT NULL,
    rating_key          TEXT NOT NULL,
    title               TEXT NOT NULL,
    player_title        TEXT NOT NULL,
    player_product      TEXT NOT NULL,
    player_platform     TEXT NOT NULL,
    video_decision      TEXT NOT NULL,
    audio_decision      TEXT,
    subtitle_decision   TEXT,
    actual              INTEGER NOT NULL,
    profile_id          TEXT,
    predicted           INTEGER,
    predicted_codes     TEXT,
    hints               TEXT,
    session             TEXT NOT NULL
);

-- Explicit Plex player title -> client profile (id or .toml path).
CREATE TABLE IF NOT EXISTS client_map (
    player_title  TEXT PRIMARY KEY COLLATE NOCASE,
    profile       TEXT NOT NULL
);
"""


@dataclass
class Playback:
    observed_at: int
    title: str
    player_title: str
    player_product: str
    player_platform: str
    video_decision: str
    audio_decision: str | None
    subtitle_decision: str | None
    actual: Impact
    profile_id: str | None
    predicted: Impact | None
    predicted_codes: list[str]
    hints: list[str]

    @property
    def matched(self) -> bool | None:
        return None if self.predicted is None else self.predicted == self.actual


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

    # --- playbacks -------------------------------------------------------

    def add_playback(self, obs, profile_id: str | None, diagnosis, codes: list[str], hints: list[str]) -> bool:
        """Record an observation. Returns False if this exact decision was already recorded."""
        cursor = self._db.execute(
            """INSERT OR IGNORE INTO playbacks (
                dedupe_key, observed_at, rating_key, title, player_title, player_product, player_platform,
                video_decision, audio_decision, subtitle_decision, actual,
                profile_id, predicted, predicted_codes, hints, session
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                obs.dedupe_key,
                int(time.time()),
                obs.rating_key,
                obs.title,
                obs.player.title,
                obs.player.product,
                obs.player.platform,
                obs.video_decision,
                obs.audio_decision,
                obs.subtitle_decision,
                int(obs.actual),
                profile_id,
                int(diagnosis.impact) if diagnosis else None,
                json.dumps(codes),
                json.dumps(hints),
                json.dumps(obs.raw),
            ),
        )
        self._db.commit()
        return cursor.rowcount == 1

    def playbacks(self, limit: int = 50, mismatches_only: bool = False) -> list[Playback]:
        where = "WHERE predicted IS NOT NULL AND predicted != actual" if mismatches_only else ""
        rows = self._db.execute(
            f"""SELECT observed_at, title, player_title, player_product, player_platform,
                       video_decision, audio_decision, subtitle_decision, actual,
                       profile_id, predicted, predicted_codes, hints
                FROM playbacks {where} ORDER BY observed_at DESC, id DESC LIMIT ?""",
            (limit,),
        )
        return [
            Playback(
                *row[:8],
                actual=Impact(row[8]),
                profile_id=row[9],
                predicted=None if row[10] is None else Impact(row[10]),
                predicted_codes=json.loads(row[11] or "[]"),
                hints=json.loads(row[12] or "[]"),
            )
            for row in rows
        ]

    def seen_players(self) -> list[tuple[str, str, str, int]]:
        """(title, product, platform, playback count) for every player observed."""
        return self._db.execute(
            """SELECT player_title, player_product, player_platform, COUNT(*)
               FROM playbacks GROUP BY player_title, player_product, player_platform ORDER BY player_title"""
        ).fetchall()

    # --- client mappings -------------------------------------------------

    def client_mappings(self) -> dict[str, str]:
        return {title.lower(): profile for title, profile in self._db.execute("SELECT * FROM client_map")}

    def map_client(self, player_title: str, profile: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO client_map VALUES (?, ?)", (player_title, profile))
        self._db.commit()

    def unmap_client(self, player_title: str) -> bool:
        cursor = self._db.execute("DELETE FROM client_map WHERE player_title = ?", (player_title,))
        self._db.commit()
        return cursor.rowcount == 1
