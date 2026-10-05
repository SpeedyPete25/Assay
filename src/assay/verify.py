"""Check a client profile against Plex's own decisions, without playing anything."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from assay.diagnose import Diagnosis, diagnose_version
from assay.models import MediaItem, MediaVersion
from assay.plex.client import PlexClient, PlexError
from assay.plex.decision import ClientIdentity, PlexDecision
from assay.profiles import Profile
from assay.watch import Observation, Player, explain_mismatch, with_selection


@dataclass
class Check:
    item: MediaItem
    version: MediaVersion
    decision: PlexDecision | None = None
    diagnosis: Diagnosis | None = None
    hints: tuple[str, ...] = ()
    error: str | None = None

    @property
    def matched(self) -> bool | None:
        if self.decision is None or self.diagnosis is None:
            return None
        return self.decision.impact == self.diagnosis.impact


def _observation(
    item: MediaItem, version: MediaVersion, decision: PlexDecision, identity: ClientIdentity, subtitles: str
) -> Observation:
    """Express Plex's decision as an Observation so playback comparison logic can be reused."""
    audio = version.active_audio
    # "auto" means the account's own subtitle selection, which the cached metadata also records.
    subtitle = next((s for s in version.subtitles if s.selected), None) if subtitles == "auto" else None
    return Observation(
        session_id="decision",
        rating_key=item.rating_key,
        title=item.title,
        player=Player("", identity.product, identity.platform, identity.device),
        version_id=version.id,
        audio_stream_id=audio.id if audio else None,
        subtitle_stream_id=subtitle.id if subtitle else None,
        video_decision="directplay" if decision.direct_play else (decision.video_decision or "copy"),
        audio_decision=decision.audio_decision,
        subtitle_decision=decision.subtitle_decision if subtitle else None,
    )


def check_version(
    plex: PlexClient,
    item: MediaItem,
    media_index: int,
    profile: Profile,
    identity: ClientIdentity,
    *,
    subtitles: str = "auto",
) -> Check:
    version = item.versions[media_index]
    check = Check(item, version)
    try:
        check.decision = plex.decision(
            item.rating_key,
            identity,
            media_index=media_index,
            subtitles=subtitles,
            max_bitrate_kbps=profile.max_bitrate_kbps,
        )
    except PlexError as e:
        check.error = str(e)
        return check

    obs = _observation(item, version, check.decision, identity, subtitles)
    selected = with_selection(version, obs.audio_stream_id, obs.subtitle_stream_id)
    check.diagnosis = diagnose_version(item, selected, profile)
    check.hints = tuple(explain_mismatch(obs, check.diagnosis))
    return check


def verify(
    plex: PlexClient,
    items: Iterable[MediaItem],
    profile: Profile,
    identity: ClientIdentity,
    *,
    subtitles: str = "auto",
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Check]:
    """Ask Plex about every version of every item and compare with Assay's prediction."""
    jobs = [(item, i) for item in items for i in range(len(item.versions))]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(check_version, plex, item, i, profile, identity, subtitles=subtitles) for item, i in jobs
        ]
        checks = []
        for done, future in enumerate(futures, start=1):
            checks.append(future.result())
            if on_progress:
                on_progress(done, len(futures))
    return checks
