import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def movie_metadata() -> dict:
    data = json.loads((FIXTURES / "plex_movie_dv_dts_pgs.json").read_text())
    return data["MediaContainer"]["Metadata"][0]


def make_metadata(rating_key="1", streams=(), container="mkv", bitrate=8000, duration=3_600_000, **extra) -> dict:
    """Build a minimal Plex metadata dict for rule tests."""
    return {
        "ratingKey": rating_key,
        "type": "movie",
        "title": f"Movie {rating_key}",
        "updatedAt": 1,
        **extra,
        "Media": [
            {
                "id": int(rating_key),
                "container": container,
                "bitrate": bitrate,
                "duration": duration,
                "Part": [{"id": 1, "file": f"/m/{rating_key}.{container}", "size": 1000, "Stream": list(streams)}],
            }
        ],
    }


def h264(**kw):
    return {"id": 1, "streamType": 1, "index": 0, "codec": "h264", "bitDepth": 8, "level": 41, **kw}


def aac(**kw):
    return {"id": 2, "streamType": 2, "index": 1, "codec": "aac", "channels": 2, "default": True, **kw}
