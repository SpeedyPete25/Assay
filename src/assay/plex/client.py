"""Minimal Plex Media Server API client (JSON responses)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from assay import __version__
from assay.plex.decision import ClientIdentity, PlexDecision, decision_params, parse_decision

# Plex library "type" numbers used by /library/sections/{id}/all?type=N
TYPE_MOVIE = 1
TYPE_EPISODE = 4
SECTION_ITEM_TYPES = {"movie": TYPE_MOVIE, "show": TYPE_EPISODE}


class PlexError(RuntimeError):
    pass


class PlexClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "X-Plex-Token": token,
                "X-Plex-Product": "Assay",
                "X-Plex-Version": __version__,
                "X-Plex-Client-Identifier": "assay-cli",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PlexClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, endpoint: str, params: dict | None = None, headers: dict | None = None) -> dict:
        try:
            response = self._http.get(endpoint, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise PlexError(f"Could not reach Plex at {self._http.base_url}: {e}") from e
        if response.status_code == 401:
            raise PlexError("Plex rejected the token (401). Check ASSAY_PLEX_TOKEN.")
        if response.is_error:
            detail = response.text.strip()[:200]
            raise PlexError(f"GET {endpoint} failed: HTTP {response.status_code}" + (f" ({detail})" if detail else ""))
        return response.json().get("MediaContainer", {})

    def sections(self) -> list[dict]:
        """Library sections, e.g. {"key": "1", "title": "Movies", "type": "movie"}."""
        return self._get("/library/sections").get("Directory", [])

    def iter_items(self, section_key: str, item_type: int, page_size: int = 200) -> Iterator[dict]:
        """Lightweight listing (ratingKey, updatedAt, title...) without stream details."""
        start = 0
        while True:
            container = self._get(
                f"/library/sections/{section_key}/all",
                {"type": item_type, "X-Plex-Container-Start": start, "X-Plex-Container-Size": page_size},
            )
            items = container.get("Metadata", [])
            yield from items
            start += len(items)
            total = container.get("totalSize", container.get("size", 0))
            if not items or start >= total:
                return

    def sessions(self) -> list[dict]:
        """Active playback sessions with Player and TranscodeSession details."""
        return self._get("/status/sessions").get("Metadata", [])

    def metadata(self, rating_key: str) -> dict:
        """Full metadata for one item, including Media/Part/Stream."""
        items = self._get(f"/library/metadata/{rating_key}").get("Metadata", [])
        if not items:
            raise PlexError(f"No metadata for ratingKey {rating_key}")
        return items[0]

    def decision(
        self,
        rating_key: str,
        identity: ClientIdentity,
        *,
        media_index: int = 0,
        subtitles: str = "auto",
        max_bitrate_kbps: int | None = None,
    ) -> PlexDecision:
        """What Plex would do if `identity` played this item now."""
        params = decision_params(rating_key, media_index, subtitles=subtitles, max_bitrate_kbps=max_bitrate_kbps)
        container = self._get("/video/:/transcode/universal/decision", params, identity.headers())
        return parse_decision(container)
