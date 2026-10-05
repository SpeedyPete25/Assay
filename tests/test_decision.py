import httpx
import pytest

from assay.models import Impact
from assay.plex import PlexClient, PlexError
from assay.plex.decision import ClientIdentity, decision_params, parse_decision
from assay.plex.parse import parse_metadata
from assay.profiles import load_profile
from assay.verify import check_version, verify

LG = ClientIdentity(product="Plex for LG", platform="webOS", device="LG TV")


def decision_container(part_decision, streams, *, direct_play_code=3000, text="App cannot direct play this item."):
    """A decision MediaContainer shaped like Plex's response."""
    return {
        "generalDecisionCode": 1001,
        "generalDecisionText": "Direct play not available; Conversion OK.",
        "directPlayDecisionCode": direct_play_code,
        "directPlayDecisionText": text,
        "transcodeDecisionCode": 1001,
        "transcodeDecisionText": "Direct play not available; Conversion OK.",
        "Metadata": [{"Media": [{"selected": True, "Part": [{"decision": part_decision, "Stream": streams}]}]}],
    }


DIRECT_PLAY = decision_container(
    "directplay",
    [{"id": 1, "streamType": 1}, {"id": 3, "streamType": 2}],
    direct_play_code=1000,
    text="Direct play OK.",
)
AUDIO_TRANSCODE = decision_container(
    "transcode",
    [{"id": 1, "streamType": 1, "decision": "copy"}, {"id": 2, "streamType": 2, "decision": "transcode"}],
    text="App cannot direct play this item. Audio codec dca is not supported.",
)
SUBTITLE_BURN = decision_container(
    "transcode",
    [
        {"id": 1, "streamType": 1, "decision": "transcode"},
        {"id": 3, "streamType": 2, "decision": "copy"},
        {"id": 4, "streamType": 3, "decision": "burn"},
    ],
)


def test_parse_direct_play():
    d = parse_decision(DIRECT_PLAY)
    assert d.direct_play and d.impact == Impact.DIRECT_PLAY
    assert d.reason == "Direct play OK."  # fallback text about conversion isn't relevant


def test_parse_audio_transcode():
    d = parse_decision(AUDIO_TRANSCODE)
    assert (d.video_decision, d.audio_decision) == ("copy", "transcode")
    assert d.impact == Impact.DIRECT_STREAM
    assert d.reason == (
        "App cannot direct play this item. Audio codec dca is not supported. Direct play not available; Conversion OK."
    )


def test_parse_subtitle_burn():
    assert parse_decision(SUBTITLE_BURN).impact == Impact.TRANSCODE


def test_falls_back_to_codes_without_part_decision():
    container = {"directPlayDecisionCode": 1000, "Metadata": [{"Media": [{"Part": [{}]}]}]}
    assert parse_decision(container).impact == Impact.DIRECT_PLAY
    assert parse_decision({}).impact == Impact.DIRECT_STREAM


def test_decision_params():
    params = decision_params("1234", 1, subtitles="none", max_bitrate_kbps=8000)
    assert params["path"] == "/library/metadata/1234"
    assert (params["mediaIndex"], params["subtitles"], params["maxVideoBitrate"]) == (1, "none", 8000)
    assert "maxVideoBitrate" not in decision_params("1234")


def fake_plex(container, seen=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/video/:/transcode/universal/decision":
            return httpx.Response(status, json={"MediaContainer": container})
        return httpx.Response(404)

    return PlexClient("http://plex:32400", "token", transport=httpx.MockTransport(handler))


def test_client_sends_identity_headers():
    seen = []
    fake_plex(DIRECT_PLAY, seen).decision("1234", LG)
    [request] = seen
    assert request.headers["X-Plex-Product"] == "Plex for LG"  # overrides the default "Assay"
    assert request.headers["X-Plex-Platform"] == "webOS"
    assert request.headers["X-Plex-Token"] == "token"
    assert request.url.params["path"] == "/library/metadata/1234"


def test_client_error_includes_body():
    with pytest.raises(PlexError, match="HTTP 400"):
        fake_plex({}, status=400).decision("1234", LG)


def test_check_matches_when_plex_agrees(movie_metadata):
    # Fixture: DTS is the selected audio, no subtitle selected.
    item = parse_metadata(movie_metadata)
    c = check_version(fake_plex(AUDIO_TRANSCODE), item, 0, load_profile("lg-webos"), LG)
    assert c.matched and c.hints == ()
    assert c.diagnosis.impact == Impact.DIRECT_STREAM


def test_check_explains_disagreement(movie_metadata):
    item = parse_metadata(movie_metadata)
    c = check_version(fake_plex(DIRECT_PLAY), item, 0, load_profile("lg-webos"), LG)
    assert c.matched is False
    [hint] = c.hints
    assert "dts" in hint and "Add it to profile 'lg-webos'" in hint


def test_verify_collects_errors(movie_metadata):
    item = parse_metadata(movie_metadata)
    [c] = verify(fake_plex({}, status=500), [item], load_profile("lg-webos"), LG)
    assert c.error and c.matched is None
