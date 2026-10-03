"""Diagnostic rules. Each rule inspects one media version against one client
profile and yields Findings. Rules are pure: no I/O, no Plex calls.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from assay.models import (
    STYLED_SUBTITLES,
    AudioStream,
    Finding,
    Impact,
    MediaVersion,
)
from assay.profiles import Profile

Rule = Callable[[MediaVersion, Profile], Iterable[Finding]]

# Text formats Plex converts for the client without touching the video.
CONVERTIBLE_TEXT_SUBTITLES = frozenset({"srt", "webvtt", "mov_text", "ttml"})

PASSTHROUGH_HINT = (
    "If the client feeds an AV receiver or soundbar, enabling audio passthrough "
    "in the Plex app may let it direct play."
)


def check_health(version: MediaVersion, profile: Profile) -> Iterable[Finding]:
    if not version.files:
        yield Finding("health.no_file", "health", None, "Plex has no file on disk for this item.")
    if version.size_bytes == 0:
        yield Finding("health.empty_file", "health", None, "File is 0 bytes.")
    if not version.duration_ms:
        yield Finding(
            "health.no_duration",
            "health",
            None,
            "No duration reported; the file may be truncated or failed analysis.",
            fix="Run 'Analyze' on the item in Plex; if it persists, deep-scan the file.",
        )
    if not version.video:
        yield Finding("health.no_video", "health", None, "No video stream.")
    if not version.audio:
        yield Finding("health.no_audio", "health", None, "No audio track.")


def check_container(version: MediaVersion, profile: Profile) -> Iterable[Finding]:
    if version.container not in profile.containers:
        yield Finding(
            f"container.unsupported:{version.container}",
            "container",
            Impact.DIRECT_STREAM,
            f"Container '{version.container}' isn't supported, so Plex remuxes it on the fly.",
            fix="Remux to MKV or MP4 (ffmpeg -c copy); no quality loss.",
        )


def check_video(version: MediaVersion, profile: Profile) -> Iterable[Finding]:
    for v in version.video[:1]:  # Plex only plays the first video stream
        limits = profile.video.get(v.codec)
        if limits is None:
            yield Finding(
                f"video.codec_unsupported:{v.codec}",
                "video",
                Impact.TRANSCODE,
                f"Video codec '{v.codec}' can't be played by this client.",
                fix=f"Re-encode to a supported codec ({', '.join(sorted(profile.video)) or 'none listed'}).",
                stream_id=v.id,
            )
            continue

        if limits.max_bit_depth and v.bit_depth and v.bit_depth > limits.max_bit_depth:
            yield Finding(
                f"video.bit_depth:{v.codec}:{v.bit_depth}",
                "video",
                Impact.TRANSCODE,
                f"{v.bit_depth}-bit {v.codec} (e.g. Hi10P) exceeds the client's {limits.max_bit_depth}-bit limit.",
                fix="Re-encode to 10-bit HEVC, which most modern clients decode in hardware.",
                stream_id=v.id,
            )

        if limits.max_level and v.level and v.level > limits.max_level:
            yield Finding(
                f"video.level:{v.codec}",
                "video",
                Impact.TRANSCODE,
                f"{v.codec} level {v.level / 10:.1f} exceeds the client's limit of {limits.max_level / 10:.1f}.",
                fix="Re-encode at a lower level.",
                stream_id=v.id,
            )

        yield from _check_hdr(v, profile)


def _check_hdr(v, profile: Profile) -> Iterable[Finding]:
    if v.hdr is None:
        return

    if v.hdr == "dolby_vision":
        if v.dovi_profile in profile.dovi_profiles and "dolby_vision" in profile.hdr:
            return
        # Profiles 7 and 8 carry an HDR10-compatible base layer; profile 5 does not.
        if v.dovi_profile in (7, 8) and "hdr10" in profile.hdr:
            yield Finding(
                f"video.dovi_fallback:{v.dovi_profile}",
                "video",
                None,
                f"Dolby Vision profile {v.dovi_profile} isn't supported; plays as HDR10 instead.",
                stream_id=v.id,
            )
            return
        yield Finding(
            f"video.dovi_unsupported:{v.dovi_profile}",
            "video",
            Impact.TRANSCODE,
            f"Dolby Vision profile {v.dovi_profile} has no fallback this client can use, so Plex "
            "must tone-map it (or colours come out purple/green).",
            fix="Use a DV-capable client, or convert to HDR10 (e.g. dovi_tool + re-encode).",
            stream_id=v.id,
        )
        return

    if v.hdr not in profile.hdr:
        yield Finding(
            f"video.hdr_unsupported:{v.hdr}",
            "video",
            Impact.TRANSCODE,
            f"{v.hdr.upper()} isn't supported, so Plex tone-maps to SDR.",
            fix="Play on an HDR-capable client, or keep an SDR version alongside.",
            stream_id=v.id,
        )


def _audio_ok(a: AudioStream, profile: Profile) -> bool:
    limits = profile.audio.get(a.codec)
    if limits is None:
        return False
    return not (limits.max_channels and a.channels and a.channels > limits.max_channels)


def _describe_audio(a: AudioStream) -> str:
    return a.title or f"{a.codec} {a.channels or '?'}ch"


def check_audio(version: MediaVersion, profile: Profile) -> Iterable[Finding]:
    active = version.active_audio
    if active is None or _audio_ok(active, profile):
        return

    if active.codec not in profile.audio:
        code = f"audio.codec_unsupported:{active.codec}"
        problem = f"Audio '{_describe_audio(active)}' uses {active.codec}, which this client can't decode."
    else:
        code = f"audio.channels:{active.codec}"
        problem = (
            f"Audio '{_describe_audio(active)}' has {active.channels} channels; "
            f"the client supports {profile.audio[active.codec].max_channels} for {active.codec}."
        )

    alternative = next((a for a in version.audio if a is not active and _audio_ok(a, profile)), None)
    if alternative:
        fix = f"Select the '{_describe_audio(alternative)}' track instead; that direct plays."
    else:
        fix = "Add a compatible AC3/EAC3/AAC track (remux, no video re-encode)."
    if active.codec in ("dts", "truehd"):
        fix += " " + PASSTHROUGH_HINT

    yield Finding(code, "audio", Impact.DIRECT_STREAM, problem + " Plex transcodes the audio.", fix=fix, stream_id=active.id)


def check_subtitles(version: MediaVersion, profile: Profile) -> Iterable[Finding]:
    for s in version.subtitles:
        if s.codec in profile.subtitles or s.codec in CONVERTIBLE_TEXT_SUBTITLES:
            continue

        label = s.title or s.language or f"#{s.id}"
        if s.is_image:
            code = f"subtitle.image_burn:{s.codec}"
            message = f"{s.codec.upper()} subtitle '{label}' is image-based; this client can't draw it, so Plex burns it into the video."
            fix = "Convert to SRT (OCR with Subtitle Edit), or keep this subtitle off."
        elif s.codec in STYLED_SUBTITLES:
            code = f"subtitle.styled_burn:{s.codec}"
            message = f"Styled {s.codec.upper()} subtitle '{label}' gets burned in to preserve styling."
            fix = "Set the client's 'Burn subtitles' option to 'Only image formats' (styling is lost), or convert to SRT."
        else:
            code = f"subtitle.unsupported:{s.codec}"
            message = f"Subtitle '{label}' uses {s.codec}, which this client can't render, so Plex burns it in."
            fix = "Convert to SRT."

        if s.selected:
            condition = None  # it's on for the token's user, so it applies now
        elif s.forced:
            condition = "when forced subtitles are shown (often automatic)"
        else:
            condition = "when this subtitle is enabled"

        yield Finding(code, "subtitle", Impact.TRANSCODE, message, fix=fix, stream_id=s.id, condition=condition)


def check_bitrate(version: MediaVersion, profile: Profile) -> Iterable[Finding]:
    limit = profile.max_bitrate_kbps
    if limit and version.bitrate_kbps and version.bitrate_kbps > limit:
        yield Finding(
            "bitrate.over_limit",
            "bitrate",
            Impact.TRANSCODE,
            f"Bitrate {version.bitrate_kbps / 1000:.1f} Mbps exceeds the {limit / 1000:.1f} Mbps limit.",
            fix="Raise the client's quality setting to Original/Maximum, or keep a lower-bitrate version.",
        )


RULES: list[Rule] = [
    check_health,
    check_container,
    check_video,
    check_audio,
    check_subtitles,
    check_bitrate,
]
