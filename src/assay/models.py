"""Normalized media model shared by every source (Plex API now, ffprobe later).

Codec names are normalized to one vocabulary (see CODEC_ALIASES) so rules and
client profiles never have to care where the data came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

CODEC_ALIASES = {
    # video
    "h265": "hevc",
    "x265": "hevc",
    "avc": "h264",
    "x264": "h264",
    "mpeg2": "mpeg2video",
    # audio
    "dca": "dts",
    "dts-hd": "dts",
    "mlp": "truehd",
    "e-ac3": "eac3",
    "ac-3": "ac3",
    # subtitles
    "subrip": "srt",
    "hdmv_pgs_subtitle": "pgs",
    "dvd_subtitle": "vobsub",
    "dvdsub": "vobsub",
    "dvb_subtitle": "dvbsub",
    "ssa": "ass",
    "vtt": "webvtt",
}

IMAGE_SUBTITLES = frozenset({"pgs", "vobsub", "dvbsub"})
STYLED_SUBTITLES = frozenset({"ass"})


def normalize_codec(codec: str | None) -> str:
    if not codec:
        return "unknown"
    codec = codec.lower()
    return CODEC_ALIASES.get(codec, codec)


class Impact(IntEnum):
    """How much work Plex has to do. Ordered so max() gives the worst outcome."""

    DIRECT_PLAY = 0
    DIRECT_STREAM = 1  # remux and/or audio transcode; video passes through
    TRANSCODE = 2  # video transcode (includes subtitle burn-in)

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()


@dataclass
class VideoStream:
    id: int
    codec: str
    profile: str | None = None
    level: int | None = None
    bit_depth: int | None = None
    width: int | None = None
    height: int | None = None
    hdr: str | None = None  # "hdr10", "hlg", "dolby_vision"
    dovi_profile: int | None = None


@dataclass
class AudioStream:
    id: int
    codec: str
    profile: str | None = None
    channels: int | None = None
    language: str | None = None
    title: str | None = None
    default: bool = False
    selected: bool = False


@dataclass
class SubtitleStream:
    id: int
    codec: str
    language: str | None = None
    title: str | None = None
    forced: bool = False
    default: bool = False
    selected: bool = False
    external: bool = False

    @property
    def is_image(self) -> bool:
        return self.codec in IMAGE_SUBTITLES


@dataclass
class MediaVersion:
    """One playable version of an item (Plex 'Media'). Items can have several."""

    id: int
    container: str
    bitrate_kbps: int | None = None
    duration_ms: int | None = None
    files: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    video: list[VideoStream] = field(default_factory=list)
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleStream] = field(default_factory=list)

    @property
    def active_audio(self) -> AudioStream | None:
        """The audio track Plex will play: selected, else default, else first."""
        for pick in (lambda a: a.selected, lambda a: a.default):
            for stream in self.audio:
                if pick(stream):
                    return stream
        return self.audio[0] if self.audio else None


@dataclass
class MediaItem:
    rating_key: str
    title: str
    type: str  # "movie" or "episode"
    library: str | None = None
    updated_at: int | None = None
    versions: list[MediaVersion] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    """One reason a file won't direct play, or a health problem with it.

    `code` is a stable key used to group findings across the library.
    `impact` is None for health problems that don't affect the play decision.
    `condition` is set when the finding only applies in some situations,
    e.g. "when this subtitle is enabled".
    """

    code: str
    category: str  # container, video, audio, subtitle, bitrate, health
    impact: Impact | None
    message: str
    fix: str | None = None
    stream_id: int | None = None
    condition: str | None = None
