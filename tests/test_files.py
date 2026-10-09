import json
import os
import subprocess

import pytest
from conftest import aac, h264, make_metadata

from assay.diagnose import diagnose
from assay.ffmpeg import MAX_STORED_ERRORS, ToolNotFound, decode_check, ffprobe, find_tool
from assay.filescan import deep_scan, file_jobs, probe_files
from assay.health import DecodeRecord, ProbeRecord, augment, enrich, file_findings
from assay.paths import common_roots, to_local
from assay.plex.parse import parse_metadata
from assay.profiles import load_profile
from assay.store import Store

# --- paths -------------------------------------------------------------------


def test_linux_plex_path_to_unc_share():
    assert to_local("/media/movies/Film (2020)/Film.mkv", {"/media": r"\\NAS\media"}) == (
        r"\\NAS\media\movies\Film (2020)\Film.mkv"
    )


def test_longest_prefix_wins_and_respects_folder_boundaries():
    mappings = {"/media": "/mnt/a", "/media/tv": "/mnt/tv"}
    assert to_local("/media/tv/Show/e1.mkv", mappings) == "/mnt/tv/Show/e1.mkv"
    assert to_local("/media2/x.mkv", mappings) == "/media2/x.mkv"  # not under /media


def test_windows_plex_paths_match_case_insensitively():
    assert to_local(r"D:\Movies\Film.mkv", {"d:/movies": r"\\SERVER\Movies"}) == r"\\SERVER\Movies\Film.mkv"


def test_unmapped_paths_pass_through():
    assert to_local("/media/x.mkv", {}) == "/media/x.mkv"


def test_common_roots():
    roots = common_roots(["/media/movies/a.mkv", "/media/movies/b.mkv", "/media/tv/s/e.mkv", r"\\NAS\share\Movies\c.mkv"])
    assert roots[0] == ("/media/movies", 2)
    assert ("/media/tv", 1) in roots
    assert (r"\\NAS\share\Movies", 1) in roots


# --- ffmpeg wrappers -----------------------------------------------------------

PROBE_JSON = {
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "hevc",
            "pix_fmt": "yuv420p10le",
            "color_transfer": "smpte2084",
            "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8}],
        },
        {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6},
    ],
    "format": {"duration": "3600.000", "size": "1000"},
}


def fake_run(outcomes):
    """A subprocess.run stand-in. outcomes maps a substring of the file path to (rc, stdout, stderr)."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        path = cmd[cmd.index("-i") + 1] if "-i" in cmd else cmd[-1]
        for key, (rc, out, err) in outcomes.items():
            if key in path:
                if rc == "timeout":
                    raise subprocess.TimeoutExpired(cmd, 1)
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(PROBE_JSON), "")

    run.calls = calls
    return run


def test_ffprobe_ok():
    outcome = ffprobe("good.mkv", binary="ffprobe", run=fake_run({}))
    assert outcome.ok and outcome.error is None
    assert outcome.data["format"]["duration"] == "3600.000"


def test_ffprobe_failure_keeps_stderr():
    run = fake_run({"bad": (1, "", "bad.mp4: moov atom not found\n")})
    outcome = ffprobe("bad.mp4", binary="ffprobe", run=run)
    assert not outcome.ok and "moov atom not found" in outcome.error


def test_ffprobe_warnings_with_success():
    run = fake_run({"warn": (0, json.dumps(PROBE_JSON), "Truncating packet of size 1024\n")})
    outcome = ffprobe("warn.mkv", binary="ffprobe", run=run)
    assert outcome.ok and outcome.error.startswith("Truncating")


def test_ffprobe_timeout():
    outcome = ffprobe("slow.mkv", binary="ffprobe", run=fake_run({"slow": ("timeout", "", "")}))
    assert not outcome.ok and "timed out" in outcome.error


def test_decode_quick_vs_full_commands():
    run = fake_run({})
    decode_check("a.mkv", binary="ffmpeg", run=run)
    decode_check("a.mkv", binary="ffmpeg", full=True, run=run)
    quick, full = run.calls
    assert quick[quick.index("-c") + 1] == "copy"
    assert "-c" not in full
    assert quick[-3:] == ["-f", "null", "-"]


def test_decode_errors_are_counted_and_capped():
    stderr = "\n".join(f"[hevc @ 0x1] corrupt frame {i}" for i in range(50))
    outcome = decode_check("broken.mkv", binary="ffmpeg", run=fake_run({"broken": (1, "", stderr)}))
    assert outcome.error_count == 51  # 50 lines + the exit code
    assert len(outcome.errors) == MAX_STORED_ERRORS


def test_find_tool(monkeypatch):
    monkeypatch.setenv("ASSAY_FFPROBE", r"C:\tools\ffprobe.exe")
    assert find_tool("ffprobe") == r"C:\tools\ffprobe.exe"
    monkeypatch.delenv("ASSAY_FFPROBE")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ToolNotFound, match="winget"):
        find_tool("ffprobe")


# --- health ----------------------------------------------------------------------


def version_for(file="/m/1.mkv", **meta):
    metadata = make_metadata("1", [h264(codec="hevc", bitDepth=None), aac()], **meta)
    metadata["Media"][0]["Part"][0]["file"] = file
    return parse_metadata(metadata).versions[0]


def probe_record(file="/m/1.mkv", **kw):
    defaults = dict(local_path=file, size=1000, mtime=1, ok=True, missing=False, error=None, data=PROBE_JSON)
    return ProbeRecord(file, **{**defaults, **kw})


def test_enrich_fills_gaps_from_ffprobe():
    v = enrich(version_for(), {"/m/1.mkv": probe_record()}).video[0]
    assert (v.bit_depth, v.dovi_profile, v.hdr) == (10, 8, "dolby_vision")


def test_enrich_skips_when_streams_dont_match():
    version = version_for()
    other = {**PROBE_JSON, "streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}]}
    assert enrich(version, {"/m/1.mkv": probe_record(data=other)}) is version


def codes(findings):
    return {f.code for f in findings}


def test_clean_probe_has_no_findings():
    assert file_findings(version_for(), {"/m/1.mkv": probe_record()}, {}) == []


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (dict(ok=False, missing=True, error="File not found", data=None), "health.missing_file"),
        (dict(ok=False, error="moov atom not found", data=None), "health.unreadable"),
        (dict(error="Truncating packet"), "health.probe_warnings"),
        (dict(size=999), "health.changed_since_plex_scan"),
        (dict(data={**PROBE_JSON, "format": {"duration": "1800"}}), "health.duration_mismatch"),
    ],
)
def test_probe_findings(record, code):
    assert codes(file_findings(version_for(), {"/m/1.mkv": probe_record(**record)}, {})) == {code}


def test_decode_findings():
    decode = DecodeRecord("/m/1.mkv", 1000, 1, "full", 10.0, 3, ["[hevc] corrupt frame"])
    [finding] = file_findings(version_for(), {}, {"/m/1.mkv": decode})
    assert finding.code == "health.decode_errors:full"
    assert "corrupt frame" in finding.message


def test_check_includes_health_and_uses_enriched_data():
    # Plex didn't report the DV profile or bit depth; ffprobe fills them in.
    metadata = make_metadata("1", [h264(codec="hevc", bitDepth=None), aac()])
    item = parse_metadata(metadata)
    probes = {"/m/1.mkv": probe_record(size=999)}
    item, extra = augment(item, probes, {})
    [d] = diagnose(item, load_profile("lg-webos"), extra)
    assert d.version.video[0].dovi_profile == 8
    assert "health.changed_since_plex_scan" in codes(d.findings)


# --- incremental scans ---------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    """A store with two items whose Plex paths map onto real temp files (one missing)."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "good.mkv").write_bytes(b"x" * 100)
    (media / "broken.mkv").write_bytes(b"x" * 50)
    store = Store(tmp_path / "assay.db")
    for key, name in (("1", "good"), ("2", "broken"), ("3", "gone")):
        meta = make_metadata(key, [h264(), aac()])
        meta["Media"][0]["Part"][0]["file"] = f"/plex/{name}.mkv"
        store.upsert("Movies", meta)
    store.commit()
    store.add_path_mapping("/plex", str(media))
    return store, media


def test_file_jobs_apply_mappings(library):
    store, media = library
    jobs = {j.file: j.local_path for j in file_jobs(store)}
    assert jobs["/plex/good.mkv"] == os.path.join(str(media), "good.mkv")


def test_probe_is_incremental(library):
    store, media = library
    run = fake_run({"broken": (1, "", "Invalid data found when processing input")})
    first = probe_files(store, file_jobs(store), binary="ffprobe", run=run)
    assert (first.checked, first.missing, first.skipped) == (2, 1, 0)
    assert first.problems == ["/plex/broken.mkv"]

    second = probe_files(store, file_jobs(store), binary="ffprobe", run=run)
    assert (second.checked, second.skipped) == (0, 2)

    (media / "good.mkv").write_bytes(b"y" * 200)  # file replaced
    third = probe_files(store, file_jobs(store), binary="ffprobe", run=run)
    assert third.checked == 1

    probes = store.probes()
    assert probes["/plex/gone.mkv"].missing
    assert not probes["/plex/broken.mkv"].ok
    assert probes["/plex/good.mkv"].size == 200
    assert store.probes(["/plex/good.mkv"]).keys() == {"/plex/good.mkv"}


def test_deep_scan_limit_and_modes(library):
    store, _ = library
    run = fake_run({"broken": (0, "", "[matroska] Truncated file")})
    seen = []
    quick = deep_scan(store, file_jobs(store), binary="ffmpeg", limit=1, run=run, on_result=lambda j, r: seen.append(j.file))
    assert quick.checked == 1 and quick.missing == 1

    rest = deep_scan(store, file_jobs(store), binary="ffmpeg", run=run, on_result=lambda j, r: seen.append(j.file))
    assert rest.checked == 1 and rest.skipped == 1
    assert sorted(seen) == ["/plex/broken.mkv", "/plex/good.mkv"]

    # A quick scan doesn't satisfy a request for a full one.
    full = deep_scan(store, file_jobs(store), binary="ffmpeg", full=True, run=run)
    assert full.checked == 2
    assert deep_scan(store, file_jobs(store), binary="ffmpeg", run=run).skipped == 2  # full covers quick

    decodes = store.decodes()
    assert decodes["/plex/broken.mkv"].error_count == 1 and decodes["/plex/broken.mkv"].mode == "full"
