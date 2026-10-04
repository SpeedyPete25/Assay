import copy

import httpx
import pytest

from assay.models import Impact
from assay.plex import PlexClient
from assay.plex.parse import parse_metadata
from assay.profiles import bundled_profiles, load_profile
from assay.store import Store
from assay.watch import Player, explain_mismatch, match_profile, parse_session, poll_once, predict

LG_PLAYER = {"title": "Living Room TV", "product": "Plex for LG", "platform": "webOS", "device": "OLED55C1"}


def session(movie_metadata, *, audio_id=2, subtitle_id=None, transcode=None, player=LG_PLAYER):
    """Build a /status/sessions entry for the fixture movie with the given selection."""
    meta = copy.deepcopy(movie_metadata)
    meta["sessionKey"] = "42"
    meta["Session"] = {"id": "abc123", "location": "lan"}
    meta["Player"] = player
    media = meta["Media"][0]
    media["selected"] = True
    for s in media["Part"][0]["Stream"]:
        s["selected"] = s["id"] in (audio_id, subtitle_id)
    if transcode:
        meta["TranscodeSession"] = transcode
    return meta


def test_direct_play_session(movie_metadata):
    obs = parse_session(session(movie_metadata, audio_id=3))
    assert obs.actual == Impact.DIRECT_PLAY
    assert (obs.version_id, obs.audio_stream_id, obs.subtitle_stream_id) == (5001, 3, None)
    assert obs.player.platform == "webOS"


def test_burned_subtitle_is_a_transcode(movie_metadata):
    obs = parse_session(
        session(
            movie_metadata,
            subtitle_id=4,
            transcode={"videoDecision": "transcode", "audioDecision": "transcode", "subtitleDecision": "burn"},
        )
    )
    assert obs.actual == Impact.TRANSCODE


def test_remux_only_is_direct_stream(movie_metadata):
    obs = parse_session(session(movie_metadata, transcode={"videoDecision": "copy", "audioDecision": "copy"}))
    assert obs.actual == Impact.DIRECT_STREAM


def test_ignores_music():
    assert parse_session({"type": "track", "ratingKey": 1}) is None


def test_prediction_uses_playback_selection(movie_metadata):
    item = parse_metadata(movie_metadata)
    lg = load_profile("lg-webos")

    # AC3 track, no subtitles: should direct play even though DTS is the default.
    d = predict(parse_session(session(movie_metadata, audio_id=3)), item, lg)
    assert d.impact == Impact.DIRECT_PLAY

    # DTS + PGS enabled: the subtitle burn now applies unconditionally.
    obs = parse_session(
        session(
            movie_metadata,
            subtitle_id=4,
            transcode={"videoDecision": "transcode", "audioDecision": "transcode", "subtitleDecision": "burn"},
        )
    )
    d = predict(obs, item, lg)
    assert d.impact == obs.actual == Impact.TRANSCODE
    assert explain_mismatch(obs, d) == []


def test_unexplained_video_transcode_points_at_quality_setting(movie_metadata):
    item = parse_metadata(movie_metadata)
    obs = parse_session(session(movie_metadata, audio_id=3, transcode={"videoDecision": "transcode", "audioDecision": "copy"}))
    d = predict(obs, item, load_profile("lg-webos"))
    assert d.impact == Impact.DIRECT_PLAY
    [hint] = explain_mismatch(obs, d)
    assert "quality setting" in hint


def test_audio_played_directly_suggests_widening_profile(movie_metadata):
    item = parse_metadata(movie_metadata)
    obs = parse_session(session(movie_metadata, audio_id=2))  # DTS, no TranscodeSession: e.g. passthrough
    d = predict(obs, item, load_profile("lg-webos"))
    assert d.impact == Impact.DIRECT_STREAM
    [hint] = explain_mismatch(obs, d)
    assert "Add it to profile 'lg-webos'" in hint and "dts" in hint


def test_match_profile():
    profiles = bundled_profiles()
    lg = Player("Living Room TV", "Plex for LG", "webOS", "")
    web = Player("Chrome", "Plex Web", "Chrome", "Windows")
    other = Player("Bedroom", "Plex for Roku", "Roku", "")
    assert match_profile(lg, {}, profiles) == "lg-webos"
    assert match_profile(web, {}, profiles) == "plex-web"
    assert match_profile(other, {}, profiles) is None
    assert match_profile(other, {"bedroom": "custom.toml"}, profiles) == "custom.toml"


@pytest.fixture
def plex_with_session(movie_metadata):
    sessions = [session(movie_metadata, subtitle_id=4, transcode={"videoDecision": "transcode", "audioDecision": "transcode", "subtitleDecision": "burn"})]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status/sessions":
            return httpx.Response(200, json={"MediaContainer": {"size": len(sessions), "Metadata": sessions}})
        if request.url.path == "/library/metadata/1234":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [movie_metadata]}})
        return httpx.Response(404)

    return PlexClient("http://plex:32400", "token", transport=httpx.MockTransport(handler))


def test_poll_records_once_and_fetches_unscanned_items(tmp_path, plex_with_session):
    store = Store(tmp_path / "assay.db")
    [r] = poll_once(plex_with_session, store)
    assert r.new and r.matched and r.profile_id == "lg-webos"
    assert store.get("1234") is not None  # fetched on demand, not scanned

    [again] = poll_once(plex_with_session, store)
    assert not again.new  # same playback, same decisions: not recorded twice

    [p] = store.playbacks()
    assert (p.actual, p.predicted, p.player_title) == (Impact.TRANSCODE, Impact.TRANSCODE, "Living Room TV")
    assert "subtitle.image_burn:pgs" in p.predicted_codes
    assert store.seen_players() == [("Living Room TV", "Plex for LG", "webOS", 1)]


def test_client_mapping_overrides_auto_match(tmp_path, plex_with_session):
    store = Store(tmp_path / "assay.db")
    store.map_client("living room tv", "plex-web")
    [r] = poll_once(plex_with_session, store)
    assert r.profile_id == "plex-web"
    assert store.unmap_client("Living Room TV")
