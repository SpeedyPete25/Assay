from dataclasses import replace

from conftest import aac, h264, make_metadata

from assay.diagnose import diagnose
from assay.models import Impact
from assay.plex.parse import parse_metadata
from assay.profiles import bundled_profiles, load_profile

LG = load_profile("lg-webos")


def run(metadata, profile=LG):
    [d] = diagnose(parse_metadata(metadata), profile)
    return d


def codes(d):
    return {f.code for f in d.findings}


def test_clean_file_direct_plays():
    d = run(make_metadata(streams=[h264(), aac()]))
    assert d.findings == ()
    assert d.impact == d.worst_case == Impact.DIRECT_PLAY


def test_fixture_on_lg(movie_metadata):
    d = run(movie_metadata)
    assert codes(d) == {
        "video.dovi_fallback:7",
        "audio.codec_unsupported:dts",
        "subtitle.image_burn:pgs",
    }
    # DTS is the selected track, so audio transcodes by default...
    assert d.impact == Impact.DIRECT_STREAM
    # ...and PGS subs would force a full video transcode if turned on.
    assert d.worst_case == Impact.TRANSCODE

    audio = next(f for f in d.findings if f.category == "audio")
    assert "English (AC3 5.1)" in audio.fix  # points at the compatible track
    subs = [f for f in d.findings if f.category == "subtitle"]
    assert {f.condition for f in subs} == {
        "when this subtitle is enabled",
        "when forced subtitles are shown (often automatic)",
    }


def test_selected_image_subtitle_is_unconditional():
    d = run(make_metadata(streams=[h264(), aac(), {"id": 3, "streamType": 3, "index": 2, "codec": "pgs", "selected": True}]))
    assert d.impact == Impact.TRANSCODE


def test_ass_burns_but_srt_does_not():
    d = run(
        make_metadata(
            streams=[
                h264(),
                aac(),
                {"id": 3, "streamType": 3, "index": 2, "codec": "ass"},
                {"id": 4, "streamType": 3, "index": 3, "codec": "subrip"},
            ]
        )
    )
    assert codes(d) == {"subtitle.styled_burn:ass"}


def test_dts_without_alternative_suggests_adding_track():
    d = run(make_metadata(streams=[h264(), aac(codec="dca", channels=6)]))
    [f] = d.findings
    assert f.impact == Impact.DIRECT_STREAM
    assert "Add a compatible" in f.fix
    assert "passthrough" in f.fix


def test_too_many_channels():
    d = run(make_metadata(streams=[h264(), aac(channels=8)]))
    assert codes(d) == {"audio.channels:aac"}


def test_hi10p_transcodes():
    d = run(make_metadata(streams=[h264(bitDepth=10, profile="high 10"), aac()]))
    assert codes(d) == {"video.bit_depth:h264:10"}
    assert d.impact == Impact.TRANSCODE


def test_dolby_vision_profile_5_without_support_transcodes():
    profile = replace(LG, dovi_profiles=frozenset({8}))
    d = run(
        make_metadata(streams=[{"id": 1, "streamType": 1, "codec": "hevc", "DOVIPresent": True, "DOVIProfile": 5}, aac()]),
        profile,
    )
    assert codes(d) == {"video.dovi_unsupported:5"}
    assert d.impact == Impact.TRANSCODE


def test_unsupported_video_codec_suggests_supported_ones():
    d = run(make_metadata(container="mp4", streams=[h264(codec="hevc"), aac()]), bundled_profiles()["plex-web"])
    [f] = d.findings
    assert f.code == "video.codec_unsupported:hevc"
    assert "h264" in f.fix


def test_avi_container_remuxes():
    d = run(make_metadata(container="avi", streams=[h264(), aac()]))
    assert codes(d) == {"container.unsupported:avi"}
    assert d.impact == Impact.DIRECT_STREAM


def test_bitrate_limit_from_quality_setting():
    d = run(make_metadata(bitrate=20000, streams=[h264(), aac()]), LG.with_max_bitrate(8000))
    assert codes(d) == {"bitrate.over_limit"}


def test_health_problems():
    d = run(make_metadata(duration=0, streams=[h264()]))
    assert {f.code for f in d.health_problems} == {"health.no_audio", "health.no_duration"}
    assert d.impact == Impact.DIRECT_PLAY  # health issues don't change the play decision


def test_bundled_profiles_load():
    profiles = bundled_profiles()
    assert {"lg-webos", "plex-web"} <= set(profiles)
    assert "dts" not in profiles["lg-webos"].audio
