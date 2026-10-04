"""Client profiles: what a Plex client can direct play.

Profiles are TOML files. Bundled ones live next to this module; users can pass
their own with a path. See lg-webos.toml for the format.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from assay.models import normalize_codec


@dataclass(frozen=True)
class VideoLimits:
    max_bit_depth: int | None = None
    max_level: int | None = None  # Plex style: 51 means level 5.1


@dataclass(frozen=True)
class AudioLimits:
    max_channels: int | None = None


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    notes: str = ""
    containers: frozenset[str] = frozenset()
    video: dict[str, VideoLimits] = field(default_factory=dict)
    audio: dict[str, AudioLimits] = field(default_factory=dict)
    subtitles: frozenset[str] = frozenset()  # formats the client renders itself
    hdr: frozenset[str] = frozenset()  # "hdr10", "hlg", "dolby_vision"
    dovi_profiles: frozenset[int] = frozenset()
    max_bitrate_kbps: int | None = None
    # Used by `assay plex watch` to pick a profile for a Plex player.
    match_platforms: frozenset[str] = frozenset()
    match_products: frozenset[str] = frozenset()

    def with_max_bitrate(self, kbps: int | None) -> Profile:
        return replace(self, max_bitrate_kbps=kbps) if kbps else self


def _codecs(values) -> frozenset[str]:
    return frozenset(normalize_codec(v) for v in values)


def parse_profile(data: dict) -> Profile:
    return Profile(
        id=data["id"],
        name=data.get("name", data["id"]),
        notes=data.get("notes", ""),
        containers=_codecs(data.get("containers", [])),
        video={normalize_codec(k): VideoLimits(**v) for k, v in data.get("video", {}).items()},
        audio={normalize_codec(k): AudioLimits(**v) for k, v in data.get("audio", {}).items()},
        subtitles=_codecs(data.get("subtitles", [])),
        hdr=frozenset(data.get("hdr", [])),
        dovi_profiles=frozenset(data.get("dovi_profiles", [])),
        max_bitrate_kbps=data.get("max_bitrate_kbps"),
        match_platforms=frozenset(data.get("match_platforms", [])),
        match_products=frozenset(data.get("match_products", [])),
    )


def bundled_profiles() -> dict[str, Profile]:
    profiles = {}
    for entry in resources.files(__package__).iterdir():
        if entry.name.endswith(".toml"):
            profile = parse_profile(tomllib.loads(entry.read_text(encoding="utf-8")))
            profiles[profile.id] = profile
    return profiles


def load_profile(name_or_path: str) -> Profile:
    """Load a bundled profile by id, or a TOML file by path."""
    path = Path(name_or_path)
    if path.suffix == ".toml" and path.exists():
        return parse_profile(tomllib.loads(path.read_text(encoding="utf-8")))
    profiles = bundled_profiles()
    if name_or_path not in profiles:
        known = ", ".join(sorted(profiles))
        raise KeyError(f"Unknown client profile '{name_or_path}'. Bundled profiles: {known}")
    return profiles[name_or_path]
