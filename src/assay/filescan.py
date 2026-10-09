"""Run ffprobe and deep scans over the files in the cache, incrementally.

A file is re-checked only when its size or modification time changes (or with
force). Results are saved one file at a time, so an interrupted run resumes
where it stopped.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from assay.ffmpeg import Run, decode_check, ffprobe
from assay.health import DecodeRecord, ProbeRecord
from assay.paths import to_local
from assay.store import Store


@dataclass(frozen=True)
class FileJob:
    file: str  # Plex's path
    local_path: str
    title: str


@dataclass
class FileScanSummary:
    checked: int = 0
    skipped: int = 0  # unchanged since the last check
    missing: int = 0
    problems: list[str] = field(default_factory=list)  # Plex paths with errors


def file_jobs(store: Store, library: str | None = None, query: str | None = None) -> list[FileJob]:
    mappings = store.path_mappings()
    items = store.search(query, limit=1_000_000) if query else store.items(library)
    jobs, seen = [], set()
    for item in items:
        for version in item.versions:
            for file in version.files:
                if file not in seen:
                    seen.add(file)
                    jobs.append(FileJob(file, to_local(file, mappings), item.title))
    return jobs


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, int(st.st_mtime)


Progress = Callable[[int, int], None]


def probe_files(
    store: Store,
    jobs: list[FileJob],
    *,
    binary: str,
    workers: int = 4,
    force: bool = False,
    run: Run = subprocess.run,
    on_progress: Progress | None = None,
) -> FileScanSummary:
    summary = FileScanSummary()
    known = store.probes()
    todo: list[tuple[FileJob, int, int]] = []

    for job in jobs:
        stat = _stat(job.local_path)
        if stat is None:
            store.save_probe(ProbeRecord(job.file, job.local_path, None, None, False, True, "File not found", None))
            summary.missing += 1
            continue
        previous = known.get(job.file)
        if not force and previous and not previous.missing and (previous.size, previous.mtime) == stat:
            summary.skipped += 1
            continue
        todo.append((job, *stat))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ffprobe, job.local_path, binary=binary, run=run): (job, size, mtime) for job, size, mtime in todo}
        for done, future in enumerate(as_completed(futures), start=1):
            job, size, mtime = futures[future]
            outcome = future.result()
            store.save_probe(
                ProbeRecord(job.file, job.local_path, size, mtime, outcome.ok, False, outcome.error, outcome.data)
            )
            summary.checked += 1
            if not outcome.ok or outcome.error:
                summary.problems.append(job.file)
            if on_progress:
                on_progress(done, len(todo))
    return summary


def deep_scan(
    store: Store,
    jobs: list[FileJob],
    *,
    binary: str,
    full: bool = False,
    workers: int = 1,
    force: bool = False,
    limit: int | None = None,
    run: Run = subprocess.run,
    on_progress: Progress | None = None,
    on_result: Callable[[FileJob, DecodeRecord], None] | None = None,
) -> FileScanSummary:
    """Deep-scan files that are new or changed. `limit` caps how many are scanned this run."""
    summary = FileScanSummary()
    known = store.decodes()
    todo: list[tuple[FileJob, int, int]] = []

    for job in jobs:
        stat = _stat(job.local_path)
        if stat is None:
            summary.missing += 1
            continue
        previous = known.get(job.file)
        # A full scan also covers what a quick scan checks.
        thorough_enough = previous and (previous.mode == "full" or not full)
        if not force and thorough_enough and (previous.size, previous.mtime) == stat:
            summary.skipped += 1
            continue
        todo.append((job, *stat))
    if limit is not None:
        todo = todo[:limit]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(decode_check, job.local_path, binary=binary, full=full, run=run): (job, size, mtime)
            for job, size, mtime in todo
        }
        for done, future in enumerate(as_completed(futures), start=1):
            job, size, mtime = futures[future]
            outcome = future.result()
            record = DecodeRecord(job.file, size, mtime, outcome.mode, outcome.seconds, outcome.error_count, outcome.errors)
            store.save_decode(record)
            summary.checked += 1
            if record.error_count:
                summary.problems.append(job.file)
            if on_result:
                on_result(job, record)
            if on_progress:
                on_progress(done, len(todo))
    return summary
