"""Incremental library scan: Plex API -> SQLite.

Only items whose Plex `updatedAt` changed since the last scan are re-fetched.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from assay.plex.client import SECTION_ITEM_TYPES, PlexClient
from assay.store import Store


@dataclass
class ScanResult:
    library: str
    total: int = 0
    fetched: int = 0
    removed: int = 0


def scan_library(
    plex: PlexClient,
    store: Store,
    section: dict,
    *,
    workers: int = 8,
    on_progress: Callable[[int, int], None] | None = None,
) -> ScanResult:
    library = section["title"]
    result = ScanResult(library)
    known = store.updated_at_by_key(library)

    listing = list(plex.iter_items(section["key"], SECTION_ITEM_TYPES[section["type"]]))
    result.total = len(listing)
    stale = [str(i["ratingKey"]) for i in listing if known.get(str(i["ratingKey"])) != i.get("updatedAt")]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, metadata in enumerate(pool.map(plex.metadata, stale), start=1):
            store.upsert(library, metadata)
            if on_progress:
                on_progress(done, len(stale))
    result.fetched = len(stale)

    gone = set(known) - {str(i["ratingKey"]) for i in listing}
    store.delete(gone)
    result.removed = len(gone)

    store.commit()
    return result


def scannable_sections(plex: PlexClient, names: list[str] | None = None) -> list[dict]:
    sections = [s for s in plex.sections() if s.get("type") in SECTION_ITEM_TYPES]
    if names:
        wanted = {n.lower() for n in names}
        sections = [s for s in sections if s["title"].lower() in wanted]
    return sections
