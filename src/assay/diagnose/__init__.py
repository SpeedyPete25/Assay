"""The diagnostic engine: predict how Plex will play a file on a client, and why."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from assay.diagnose.rules import RULES
from assay.models import Finding, Impact, MediaItem, MediaVersion
from assay.profiles import Profile


@dataclass(frozen=True)
class Diagnosis:
    item: MediaItem
    version: MediaVersion
    profile: Profile
    findings: tuple[Finding, ...]

    @property
    def impact(self) -> Impact:
        """Predicted decision with default settings (conditional findings excluded)."""
        return max(
            (f.impact for f in self.findings if f.impact is not None and f.condition is None),
            default=Impact.DIRECT_PLAY,
        )

    @property
    def worst_case(self) -> Impact:
        """Decision if every conditional finding applies (e.g. subtitles turned on)."""
        return max((f.impact for f in self.findings if f.impact is not None), default=Impact.DIRECT_PLAY)

    @property
    def health_problems(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.category == "health")


def diagnose_version(
    item: MediaItem, version: MediaVersion, profile: Profile, extra: Iterable[Finding] = ()
) -> Diagnosis:
    """Run every rule. `extra` adds findings from outside the rules (e.g. file health)."""
    findings = tuple(f for rule in RULES for f in rule(version, profile)) + tuple(extra)
    return Diagnosis(item, version, profile, findings)


def diagnose(
    item: MediaItem, profile: Profile, extra: dict[int, list[Finding]] | None = None
) -> list[Diagnosis]:
    """Diagnose every version of an item. `extra` maps version id to additional findings."""
    extra = extra or {}
    return [diagnose_version(item, v, profile, extra.get(v.id, ())) for v in item.versions]


__all__ = ["Diagnosis", "diagnose", "diagnose_version"]
