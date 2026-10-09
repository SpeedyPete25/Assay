"""File-level health from ffprobe and deep scans, and enrichment of Plex's data.

Plex's metadata says what a file *should* contain. ffprobe and ffmpeg say what
is actually on disk: whether it can be read, whether it has changed since Plex
analysed it, and whether every packet decodes.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from dataclasses import dataclass, replace

from assay.models import Finding, MediaItem, MediaVersion, normalize_codec
from assay.plex.parse import COLOR_TRC_HDR

DURATION_TOLERANCE_S = 10
DURATION_TOLERANCE_RATIO = 0.02


@dataclass
class ProbeRecord:
    file: str  # path as Plex reports it
    local_path: str
    size: int | None
    mtime: int | None
    ok: bool
    missing: bool
    error: str | None
    data: dict | None

    @property
    def duration_ms(self) -> int | None:
        try:
            return int(float(self.data["format"]["duration"]) * 1000)
        except (TypeError, KeyError, ValueError):
            return None

    def streams(self, codec_type: str) -> list[dict]:
        return [s for s in (self.data or {}).get("streams", []) if s.get("codec_type") == codec_type]


@dataclass
class DecodeRecord:
    file: str
    size: int | None
    mtime: int | None
    mode: str
    seconds: float
    error_count: int
    errors: list[str]


def _name(path: str) -> str:
    return ntpath.basename(path) if "\\" in path else posixpath.basename(path)


def _bit_depth(stream: dict) -> int | None:
    try:
        return int(stream["bits_per_raw_sample"])
    except (KeyError, TypeError, ValueError):
        pass
    pix_fmt = stream.get("pix_fmt") or ""
    match = re.search(r"p(\d{2})(le|be)$", pix_fmt)
    if match:
        return int(match.group(1))
    return 8 if pix_fmt else None


def _dovi_profile(stream: dict) -> int | None:
    for side_data in stream.get("side_data_list", []):
        if side_data.get("side_data_type") == "DOVI configuration record":
            return side_data.get("dv_profile")
    return None


def enrich(version: MediaVersion, probes: dict[str, ProbeRecord]) -> MediaVersion:
    """Fill gaps in Plex's first video stream from ffprobe (bit depth, HDR, Dolby Vision profile)."""
    probe = probes.get(version.files[0]) if version.files else None
    if not (probe and probe.ok and version.video):
        return version
    probed = probe.streams("video")
    if not probed:
        return version

    v, p = version.video[0], probed[0]
    if normalize_codec(p.get("codec_name")) != v.codec:
        return version  # not the same stream; don't guess

    dovi = v.dovi_profile if v.dovi_profile is not None else _dovi_profile(p)
    hdr = v.hdr or ("dolby_vision" if dovi is not None else COLOR_TRC_HDR.get(p.get("color_transfer", "")))
    enriched = replace(
        v,
        bit_depth=v.bit_depth or _bit_depth(p),
        dovi_profile=dovi,
        hdr=hdr,
    )
    if enriched == v:
        return version
    return replace(version, video=[enriched, *version.video[1:]])


def file_findings(
    version: MediaVersion, probes: dict[str, ProbeRecord], decodes: dict[str, DecodeRecord]
) -> list[Finding]:
    findings = []
    single_file = len(version.files) == 1

    for file in version.files:
        name = _name(file)
        probe = probes.get(file)
        if probe and probe.missing:
            findings.append(
                Finding(
                    "health.missing_file",
                    "health",
                    None,
                    f"'{name}' wasn't found at {probe.local_path}.",
                    fix="Check the path mappings ('assay paths'), or whether the file was moved or deleted.",
                )
            )
            continue
        if probe and not probe.ok:
            findings.append(
                Finding(
                    "health.unreadable",
                    "health",
                    None,
                    f"ffprobe couldn't read '{name}': {(probe.error or 'unknown error').splitlines()[0]}",
                    fix="The file is likely corrupt or incomplete; replace it.",
                )
            )
            continue
        if probe:
            if probe.error:
                findings.append(
                    Finding(
                        "health.probe_warnings",
                        "health",
                        None,
                        f"ffprobe reported problems reading '{name}': {probe.error.splitlines()[0]}",
                        fix="Run 'assay deepscan' on it to see whether the damage affects playback.",
                    )
                )
            if single_file and version.size_bytes and probe.size and version.size_bytes != probe.size:
                findings.append(
                    Finding(
                        "health.changed_since_plex_scan",
                        "health",
                        None,
                        f"'{name}' is {probe.size:,} bytes on disk but Plex recorded {version.size_bytes:,}.",
                        fix="Refresh the item in Plex (or run 'Analyze') so its stream details are current.",
                    )
                )
            plex_ms, disk_ms = version.duration_ms, probe.duration_ms
            if single_file and plex_ms and disk_ms:
                gap = abs(plex_ms - disk_ms) / 1000
                if gap > max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_RATIO * plex_ms / 1000):
                    findings.append(
                        Finding(
                            "health.duration_mismatch",
                            "health",
                            None,
                            f"'{name}' is {disk_ms / 60000:.1f} min on disk but Plex says {plex_ms / 60000:.1f} min.",
                            fix="A shorter file is usually truncated; run 'assay deepscan' on it.",
                        )
                    )

        decode = decodes.get(file)
        if decode and decode.error_count:
            sample = decode.errors[0] if decode.errors else ""
            findings.append(
                Finding(
                    f"health.decode_errors:{decode.mode}",
                    "health",
                    None,
                    f"Deep scan ({decode.mode}) of '{name}' found {decode.error_count} error(s), e.g. \"{sample}\".",
                    fix="Expect glitches or playback stopping. Replace the file; errors only at the end usually mean truncation.",
                )
            )
    return findings


def augment(
    item: MediaItem, probes: dict[str, ProbeRecord], decodes: dict[str, DecodeRecord]
) -> tuple[MediaItem, dict[int, list[Finding]]]:
    """Apply ffprobe enrichment to every version and collect file health findings for each."""
    versions = [enrich(v, probes) for v in item.versions]
    extra = {v.id: file_findings(v, probes, decodes) for v in versions}
    return replace(item, versions=versions), extra
