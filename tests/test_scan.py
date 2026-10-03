import httpx
from conftest import aac, h264, make_metadata

from assay.plex import PlexClient
from assay.scan import scan_library, scannable_sections
from assay.store import Store


class FakePlex:
    """httpx transport that serves a tiny Plex library and counts metadata fetches."""

    def __init__(self, items):
        self.items = {i["ratingKey"]: i for i in items}
        self.metadata_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/library/sections":
            body = {"Directory": [{"key": "1", "title": "Movies", "type": "movie"}, {"key": "2", "title": "Music", "type": "artist"}]}
        elif path == "/library/sections/1/all":
            listing = [{"ratingKey": k, "updatedAt": v["updatedAt"]} for k, v in self.items.items()]
            body = {"size": len(listing), "totalSize": len(listing), "Metadata": listing}
        elif path.startswith("/library/metadata/"):
            self.metadata_requests += 1
            body = {"Metadata": [self.items[path.rsplit("/", 1)[1]]]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"MediaContainer": body})


def test_incremental_scan(tmp_path):
    fake = FakePlex([make_metadata("1", [h264(), aac()]), make_metadata("2", [h264(), aac()])])
    plex = PlexClient("http://plex:32400", "token", transport=httpx.MockTransport(fake))
    store = Store(tmp_path / "assay.db")

    [section] = scannable_sections(plex)  # music library is skipped
    result = scan_library(plex, store, section, workers=2)
    assert (result.total, result.fetched, result.removed) == (2, 2, 0)

    # Nothing changed: no metadata refetched.
    assert scan_library(plex, store, section).fetched == 0
    assert fake.metadata_requests == 2

    # One item updated, one deleted.
    fake.items["1"]["updatedAt"] = 2
    del fake.items["2"]
    result = scan_library(plex, store, section)
    assert (result.fetched, result.removed) == (1, 1)
    assert store.count() == 1
    assert store.search("Movie 1")[0].rating_key == "1"
