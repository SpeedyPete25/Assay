"""Convert Plex `/library/metadata/{ratingKey}` JSON into the normalized model."""

from __future__ import annotations

from typing import Any

from assay.models import (
    AudioStream,
    MediaItem,
    MediaVersion,
    SubtitleStream,
    VideoStream,
    normalize_codec,
)

COLOR_TRC_HDR = {
    "smpte2084": "hdr10",
    "arib-std-b67": "hlg",
}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _video(s: dict) -> VideoStream:
    hdr = COLOR_TRC_HDR.get(str(s.get("colorTrc", "")).lower())
    if s.get("DOVIPresent"):
        hdr = "dolby_vision"
    return VideoStream(
        id=s["id"],
        codec=normalize_codec(s.get("codec")),
        profile=(s.get("profile") or None),
        level=_int(s.get("level")),
        bit_depth=_int(s.get("bitDepth")),
        width=_int(s.get("width")),
        height=_int(s.get("height")),
        hdr=hdr,
        dovi_profile=_int(s.get("DOVIProfile")),
    )


def _audio(s: dict) -> AudioStream:
    return AudioStream(
        id=s["id"],
        codec=normalize_codec(s.get("codec")),
        profile=(s.get("profile") or None),
        channels=_int(s.get("channels")),
        language=s.get("languageCode") or s.get("language"),
        title=s.get("extendedDisplayTitle") or s.get("displayTitle") or s.get("title"),
        default=bool(s.get("default")),
        selected=bool(s.get("selected")),
    )


def _subtitle(s: dict) -> SubtitleStream:
    return SubtitleStream(
        id=s["id"],
        codec=normalize_codec(s.get("codec")),
        language=s.get("languageCode") or s.get("language"),
        title=s.get("extendedDisplayTitle") or s.get("displayTitle") or s.get("title"),
        forced=bool(s.get("forced")),
        default=bool(s.get("default")),
        selected=bool(s.get("selected")),
        # Sidecar files have a stream key; embedded streams have an index instead.
        external="key" in s and "index" not in s,
    )


def _version(media: dict) -> MediaVersion:
    parts = media.get("Part", [])
    version = MediaVersion(
        id=media["id"],
        container=normalize_codec(media.get("container") or (parts[0].get("container") if parts else None)),
        bitrate_kbps=_int(media.get("bitrate")),
        duration_ms=_int(media.get("duration")),
        files=[p["file"] for p in parts if p.get("file")],
        size_bytes=sum(_int(p.get("size")) or 0 for p in parts) or None,
    )
    for part in parts:
        for s in part.get("Stream", []):
            # Plex streamType: 1 video, 2 audio, 3 subtitle
            match s.get("streamType"):
                case 1:
                    version.video.append(_video(s))
                case 2:
                    version.audio.append(_audio(s))
                case 3:
                    version.subtitles.append(_subtitle(s))
    return version


def display_title(meta: dict) -> str:
    if meta.get("type") == "episode":
        season = _int(meta.get("parentIndex")) or 0
        episode = _int(meta.get("index")) or 0
        return f"{meta.get('grandparentTitle', '?')} - S{season:02d}E{episode:02d} - {meta.get('title', '')}"
    year = meta.get("year")
    return f"{meta.get('title', '?')} ({year})" if year else meta.get("title", "?")


def parse_metadata(meta: dict) -> MediaItem:
    """Parse a single Plex Metadata object (with Media/Part/Stream children)."""
    return MediaItem(
        rating_key=str(meta["ratingKey"]),
        title=display_title(meta),
        type=meta.get("type", "unknown"),
        library=meta.get("librarySectionTitle"),
        updated_at=_int(meta.get("updatedAt")),
        versions=[_version(m) for m in meta.get("Media", [])],
    )
