"""Compare Assay's predictions with what Plex actually decided during playback.

Plex's `/status/sessions` reports, for each active stream, the player and a
TranscodeSession with per-stream decisions. We replay the same file, audio
track and subtitle choice through the diagnostic engine and record both.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from assay.diagnose import Diagnosis, diagnose_version
from assay.models import Finding, Impact, MediaItem, MediaVersion
from assay.plex.parse import display_title
from assay.profiles import Profile, bundled_profiles, load_profile


@dataclass(frozen=True)
class Player:
    title: str
    product: str
    platform: str
    device: str


@dataclass
class Observation:
    """One playback as reported by Plex."""

    session_id: str
    rating_key: str
    title: str
    player: Player
    version_id: int | None
    audio_stream_id: int | None
    subtitle_stream_id: int | None
    video_decision: str  # directplay, copy, transcode
    audio_decision: str | None
    subtitle_decision: str | None  # copy, transcode, burn
    hw_transcode: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def actual(self) -> Impact:
        if self.video_decision == "transcode" or self.subtitle_decision == "burn":
            return Impact.TRANSCODE
        if self.video_decision == "directplay":
            return Impact.DIRECT_PLAY
        return Impact.DIRECT_STREAM

    @property
    def dedupe_key(self) -> str:
        """Same playback with the same decisions is only recorded once per poll series."""
        return "|".join(
            str(x)
            for x in (
                self.session_id,
                self.rating_key,
                self.audio_stream_id,
                self.subtitle_stream_id,
                self.video_decision,
                self.audio_decision,
                self.subtitle_decision,
            )
        )


def _selected_stream(media: dict, stream_type: int) -> int | None:
    for part in media.get("Part", []):
        for s in part.get("Stream", []):
            if s.get("streamType") == stream_type and s.get("selected"):
                return s.get("id")
    return None


def parse_session(meta: dict) -> Observation | None:
    """Parse one entry of /status/sessions. Returns None for non-video sessions."""
    if meta.get("type") not in ("movie", "episode"):
        return None

    player = meta.get("Player", {})
    transcode = meta.get("TranscodeSession")
    media_list = meta.get("Media", [])
    media = next((m for m in media_list if m.get("selected")), media_list[0] if media_list else {})

    if transcode:
        video = transcode.get("videoDecision", "copy")
        audio = transcode.get("audioDecision")
        subtitle = transcode.get("subtitleDecision")
    else:
        video, audio, subtitle = "directplay", None, None

    return Observation(
        session_id=str(meta.get("Session", {}).get("id") or meta.get("sessionKey", "")),
        rating_key=str(meta["ratingKey"]),
        title=display_title(meta),
        player=Player(
            title=player.get("title", ""),
            product=player.get("product", ""),
            platform=player.get("platform", ""),
            device=player.get("device", ""),
        ),
        version_id=media.get("id"),
        audio_stream_id=_selected_stream(media, 2),
        subtitle_stream_id=_selected_stream(media, 3),
        video_decision=video,
        audio_decision=audio,
        subtitle_decision=subtitle,
        hw_transcode=bool(transcode and transcode.get("transcodeHwRequested")),
        raw=meta,
    )


def match_profile(player: Player, mappings: dict[str, str], profiles: dict[str, Profile]) -> str | None:
    """Explicit mapping by player name first, then the profiles' own match rules."""
    mapped = mappings.get(player.title.lower())
    if mapped:
        return mapped
    for profile in profiles.values():
        if player.platform.lower() in {p.lower() for p in profile.match_platforms}:
            return profile.id
        if player.product.lower() in {p.lower() for p in profile.match_products}:
            return profile.id
    return None


def with_selection(version: MediaVersion, audio_id: int | None, subtitle_id: int | None) -> MediaVersion:
    """A copy of the version with the playback's audio/subtitle choice applied."""
    audio = version.audio
    if audio_id is not None:
        audio = [replace(a, selected=a.id == audio_id) for a in version.audio]
    subtitles = [replace(s, selected=s.id == subtitle_id) for s in version.subtitles]
    return replace(version, audio=audio, subtitles=subtitles)


def predict(obs: Observation, item: MediaItem, profile: Profile) -> Diagnosis | None:
    version = next((v for v in item.versions if v.id == obs.version_id), None)
    if version is None:
        if len(item.versions) != 1:
            return None
        version = item.versions[0]
    return diagnose_version(item, with_selection(version, obs.audio_stream_id, obs.subtitle_stream_id), profile)


def applicable(d: Diagnosis) -> list[Finding]:
    """Findings that affect this playback (conditions resolved by the selection)."""
    return [f for f in d.findings if f.impact is not None and f.condition is None]


def explain_mismatch(obs: Observation, d: Diagnosis) -> list[str]:
    """Stream-level reasons the prediction and Plex disagree, phrased as profile fixes."""
    findings = applicable(d)
    predicted_video = any(f.impact == Impact.TRANSCODE for f in findings)
    predicted_audio = any(f.category == "audio" for f in findings)
    actual_video = obs.actual == Impact.TRANSCODE
    actual_audio = obs.audio_decision == "transcode"
    active_audio = next((a for a in d.version.audio if a.selected), d.version.active_audio)
    subtitle = next((s for s in d.version.subtitles if s.selected), None)
    pid = d.profile.id
    hints = []

    if actual_audio and not predicted_audio and active_audio:
        hints.append(
            f"Plex transcoded {active_audio.codec} {active_audio.channels or '?'}ch audio, but profile "
            f"'{pid}' says it's supported. Remove it or lower max_channels."
        )
    if predicted_audio and not actual_audio and active_audio:
        hints.append(
            f"The client played {active_audio.codec} {active_audio.channels or '?'}ch audio directly "
            f"(possibly passthrough). Add it to profile '{pid}'."
        )

    if actual_video and not predicted_video:
        if obs.subtitle_decision == "burn" and subtitle:
            hints.append(f"Plex burned in {subtitle.codec} subtitles; profile '{pid}' says the client renders them.")
        else:
            hints.append(
                "Plex transcoded the video with no predicted cause. Most often that's the client's quality "
                f"setting (try --max-bitrate), or a codec, level or HDR limit missing from profile '{pid}'."
            )
    if predicted_video and not actual_video:
        causes = ", ".join(f.code for f in findings if f.impact == Impact.TRANSCODE)
        hints.append(f"The client handled {causes} without a video transcode; profile '{pid}' is too strict.")

    if obs.actual == Impact.DIRECT_STREAM and d.impact == Impact.DIRECT_PLAY and not hints:
        hints.append(f"Plex remuxed the {d.version.container} container; profile '{pid}' says it's supported.")
    if obs.actual == Impact.DIRECT_PLAY and any(f.category == "container" for f in findings):
        hints.append(f"The client played {d.version.container} directly. Add it to profile '{pid}'.")

    return hints


@dataclass
class Result:
    obs: Observation
    profile_id: str | None
    diagnosis: Diagnosis | None
    hints: list[str]
    new: bool  # False if this exact decision was already recorded

    @property
    def matched(self) -> bool | None:
        return None if self.diagnosis is None else self.diagnosis.impact == self.obs.actual


def _item(plex, store, rating_key: str) -> MediaItem | None:
    """Cached item, or fetch it from Plex if it hasn't been scanned yet."""
    item = store.get(rating_key)
    if item is None:
        metadata = plex.metadata(rating_key)
        store.upsert(metadata.get("librarySectionTitle", "?"), metadata)
        store.commit()
        item = store.get(rating_key)
    return item


def poll_once(plex, store) -> list[Result]:
    """Read active sessions, predict each one, and record it."""
    profiles = bundled_profiles()
    mappings = store.client_mappings()
    results = []

    for meta in plex.sessions():
        obs = parse_session(meta)
        if obs is None:
            continue

        profile_id = match_profile(obs.player, mappings, profiles)
        diagnosis, hints = None, []
        if profile_id:
            try:
                profile = load_profile(profile_id)
            except KeyError:
                profile = None
            item = _item(plex, store, obs.rating_key)
            if profile and item:
                diagnosis = predict(obs, item, profile)
        if diagnosis:
            # Checked even when the verdicts agree: stream-level disagreements can cancel out.
            hints = explain_mismatch(obs, diagnosis)

        codes = [f.code for f in applicable(diagnosis)] if diagnosis else []
        new = store.add_playback(obs, profile_id, diagnosis, codes, hints)
        results.append(Result(obs, profile_id, diagnosis, hints, new))

    return results
