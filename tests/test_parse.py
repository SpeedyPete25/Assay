from assay.plex.parse import parse_metadata


def test_parses_fixture(movie_metadata):
    item = parse_metadata(movie_metadata)
    assert item.rating_key == "1234"
    assert item.title == "Example Movie (2021)"
    assert item.library == "Movies"

    [version] = item.versions
    assert version.container == "mkv"
    assert version.size_bytes == 40_500_000_000

    [video] = version.video
    assert (video.codec, video.bit_depth, video.hdr, video.dovi_profile) == ("hevc", 10, "dolby_vision", 7)

    dts, ac3 = version.audio
    assert dts.codec == "dts"  # Plex's "dca" is normalized
    assert version.active_audio is dts

    assert [s.codec for s in version.subtitles] == ["pgs", "pgs", "srt"]
    assert version.subtitles[1].forced
    assert version.subtitles[2].external
    assert not version.subtitles[0].external


def test_episode_title():
    item = parse_metadata(
        {"ratingKey": 9, "type": "episode", "title": "Pilot", "grandparentTitle": "Show", "parentIndex": 1, "index": 2}
    )
    assert item.title == "Show - S01E02 - Pilot"
    assert item.versions == []
