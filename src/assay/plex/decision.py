"""Ask Plex how it would play an item for a given client, without playing it.

Plex's `/video/:/transcode/universal/decision` returns the decision it would
make for a client, plus its own explanation text. Plex picks the client's
capabilities from the identity headers (product/platform), so we can ask on
behalf of a TV from any machine.

Caveat: real apps often send extra capabilities in X-Plex-Client-Profile-Extra
at playback time, so answers for an impersonated identity reflect Plex's
server-side base profile for that client, not necessarily the app itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from assay.models import Impact


@dataclass(frozen=True)
class ClientIdentity:
    """Who we claim to be when asking Plex for a decision."""

    product: str
    platform: str
    device: str = ""
    platform_version: str = ""
    profile_extra: str = ""  # optional X-Plex-Client-Profile-Extra

    def headers(self) -> dict[str, str]:
        headers = {
            "X-Plex-Product": self.product,
            "X-Plex-Platform": self.platform,
            # A stable identifier per identity, so Plex doesn't accumulate devices.
            "X-Plex-Client-Identifier": f"assay-{self.product}-{self.platform}".lower().replace(" ", "-"),
        }
        if self.device:
            headers["X-Plex-Device"] = self.device
            headers["X-Plex-Device-Name"] = f"Assay ({self.device})"
        if self.platform_version:
            headers["X-Plex-Platform-Version"] = self.platform_version
        if self.profile_extra:
            headers["X-Plex-Client-Profile-Extra"] = self.profile_extra
        return headers


@dataclass
class PlexDecision:
    general_code: int | None
    general_text: str
    direct_play_code: int | None
    direct_play_text: str
    transcode_code: int | None
    transcode_text: str
    part_decision: str | None  # directplay, transcode
    video_decision: str | None  # copy, transcode
    audio_decision: str | None
    subtitle_decision: str | None  # copy, transcode, burn, sidecar
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def direct_play(self) -> bool:
        if self.part_decision:
            return self.part_decision == "directplay"
        return self.direct_play_code == 1000

    @property
    def impact(self) -> Impact:
        if self.direct_play:
            return Impact.DIRECT_PLAY
        if self.video_decision == "transcode" or self.subtitle_decision == "burn":
            return Impact.TRANSCODE
        return Impact.DIRECT_STREAM

    @property
    def reason(self) -> str:
        """Plex's own explanation, most specific first."""
        if self.direct_play:
            return self.direct_play_text or self.general_text
        texts = [self.direct_play_text, self.transcode_text] if self.direct_play_text or self.transcode_text else [self.general_text]
        return " ".join(dict.fromkeys(t for t in texts if t))  # dedupe, keep order


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_decision(container: dict) -> PlexDecision:
    """Parse the MediaContainer of a decision response."""
    metadata = (container.get("Metadata") or [{}])[0]
    media = (metadata.get("Media") or [{}])[0]
    part = (media.get("Part") or [{}])[0]

    stream_decisions: dict[int, str | None] = {1: None, 2: None, 3: None}
    for s in part.get("Stream", []):
        kind = s.get("streamType")
        if kind in stream_decisions and stream_decisions[kind] is None and s.get("decision"):
            stream_decisions[kind] = s["decision"]

    return PlexDecision(
        general_code=_int(container.get("generalDecisionCode")),
        general_text=container.get("generalDecisionText", ""),
        direct_play_code=_int(container.get("directPlayDecisionCode")),
        direct_play_text=container.get("directPlayDecisionText", ""),
        transcode_code=_int(container.get("transcodeDecisionCode")),
        transcode_text=container.get("transcodeDecisionText", ""),
        part_decision=part.get("decision"),
        video_decision=stream_decisions[1],
        audio_decision=stream_decisions[2],
        subtitle_decision=stream_decisions[3],
        raw=container,
    )


def decision_params(
    rating_key: str,
    media_index: int = 0,
    *,
    subtitles: str = "auto",
    max_bitrate_kbps: int | None = None,
) -> dict:
    params = {
        "path": f"/library/metadata/{rating_key}",
        "mediaIndex": media_index,
        "partIndex": 0,
        "protocol": "hls",
        "directPlay": 1,
        "directStream": 1,
        "directStreamAudio": 1,
        "fastSeek": 1,
        "location": "lan",
        "subtitles": subtitles,  # auto (account's selection) or none
        "subtitleSize": 100,
        "audioBoost": 100,
        "session": f"assay-{uuid.uuid4().hex[:12]}",
    }
    if max_bitrate_kbps:
        params["maxVideoBitrate"] = max_bitrate_kbps
        params["videoQuality"] = 100
    return params
