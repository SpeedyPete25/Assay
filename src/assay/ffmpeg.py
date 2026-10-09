"""Thin wrappers around the ffprobe and ffmpeg binaries.

Commands are run through an injectable `run` function so tests don't need
ffmpeg installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

Run = Callable[..., subprocess.CompletedProcess]

MAX_STORED_ERRORS = 20


class ToolNotFound(RuntimeError):
    pass


def find_tool(name: str, override: str | None = None) -> str:
    """Locate ffprobe/ffmpeg: explicit path, then ASSAY_FFPROBE/ASSAY_FFMPEG, then PATH."""
    candidate = override or os.environ.get(f"ASSAY_{name.upper()}") or shutil.which(name)
    if not candidate:
        raise ToolNotFound(
            f"{name} not found. Install ffmpeg (e.g. 'winget install Gyan.FFmpeg') "
            f"or set ASSAY_{name.upper()} to its path."
        )
    return candidate


@dataclass
class ProbeOutcome:
    ok: bool
    data: dict | None = None
    error: str | None = None  # ffprobe's stderr; may be set even when ok (warnings)


def ffprobe(path: str, *, binary: str, timeout: float = 120, run: Run = subprocess.run) -> ProbeOutcome:
    cmd = [binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", path]
    try:
        proc = run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return ProbeOutcome(False, error=f"ffprobe timed out after {timeout:.0f}s")
    stderr = proc.stderr.strip() or None
    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        data = None
    if proc.returncode != 0 or not data or not data.get("streams"):
        return ProbeOutcome(False, data, stderr or f"ffprobe exited with code {proc.returncode} and found no streams")
    return ProbeOutcome(True, data, stderr)


@dataclass
class DecodeOutcome:
    mode: str  # "quick" (read every packet) or "full" (decode everything)
    seconds: float
    error_count: int
    errors: list[str] = field(default_factory=list)  # first MAX_STORED_ERRORS lines


def decode_check(
    path: str, *, binary: str, full: bool = False, timeout: float | None = None, run: Run = subprocess.run
) -> DecodeOutcome:
    """Read the whole file with ffmpeg and collect every error it reports.

    Quick mode remuxes to a null output: it reads and parses every packet, which
    catches truncation and container damage at disk/network speed. Full mode
    decodes video and audio too, which also catches corrupt frames, but is far
    slower (roughly real-time for 4K HEVC on a CPU).
    """
    cmd = [binary, "-nostdin", "-hide_banner", "-v", "error", "-i", path, "-map", "0:v?", "-map", "0:a?"]
    if not full:
        cmd += ["-c", "copy"]
    cmd += ["-f", "null", "-"]

    started = time.monotonic()
    try:
        proc = run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        lines = [line for line in proc.stderr.splitlines() if line.strip()]
        if proc.returncode != 0:
            lines.append(f"ffmpeg exited with code {proc.returncode}")
    except subprocess.TimeoutExpired:
        lines = [f"ffmpeg timed out after {timeout:.0f}s"]
    return DecodeOutcome(
        mode="full" if full else "quick",
        seconds=time.monotonic() - started,
        error_count=len(lines),
        errors=lines[:MAX_STORED_ERRORS],
    )
