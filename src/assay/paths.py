"""Translate the file paths Plex reports into paths this machine can open.

Plex says `/media/movies/Film.mkv`; on another PC the same file might be
`\\\\NAS\\media\\movies\\Film.mkv`. Mappings are prefix replacements, longest
prefix first. With no mapping, paths are used as-is (running on the server).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

_WINDOWS_PATH = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")


def _is_windows(path: str) -> bool:
    return bool(_WINDOWS_PATH.match(path))


def _starts_with(path: str, prefix: str) -> bool:
    """Prefix match on whole path components (so /media doesn't match /media2)."""
    if _is_windows(prefix):
        path, prefix = path.lower().replace("/", "\\"), prefix.lower().replace("/", "\\")
    prefix = prefix.rstrip("/\\")
    return path == prefix or path.startswith(prefix + ("\\" if _is_windows(prefix) else "/"))


def to_local(plex_path: str, mappings: dict[str, str]) -> str:
    """Apply the longest matching mapping; separators follow the local side."""
    for plex_prefix in sorted(mappings, key=len, reverse=True):
        if _starts_with(plex_path, plex_prefix):
            local_prefix = mappings[plex_prefix].rstrip("/\\")
            rest = plex_path[len(plex_prefix.rstrip("/\\")) :].lstrip("/\\")
            sep = "\\" if _is_windows(local_prefix) else "/"
            rest = rest.replace("\\", sep).replace("/", sep)
            return f"{local_prefix}{sep}{rest}" if rest else local_prefix
    return plex_path


def common_roots(paths: Iterable[str], depth: int = 2) -> list[tuple[str, int]]:
    """The top-level folders Plex's files live in, with file counts, to help set up mappings."""
    roots: Counter[str] = Counter()
    for path in paths:
        if _is_windows(path):
            parts = re.split(r"[\\/]", path)
            # \\server\share\... splits into ['', '', 'server', 'share', ...]
            head = 4 if path.startswith("\\\\") else 1
            roots["\\".join(parts[: head + depth - 1])] += 1
        else:
            parts = path.split("/")
            roots["/".join(parts[: depth + 1])] += 1
    return roots.most_common()
