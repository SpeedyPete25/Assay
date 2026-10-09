# Assay

Plex library health checks, and an answer to "why is this file transcoding on my TV?"

Assay reads your library through the Plex API (it doesn't need to run on the
media server) and predicts, for each file and client, whether Plex will direct
play, direct stream (remux or audio transcode only) or fully transcode it, with
the reason and the cheapest fix.

## Setup

```sh
python -m venv .venv
.venv\Scripts\activate          # Windows; use `source .venv/bin/activate` elsewhere
pip install -e ".[dev]"
```

Point it at your server ([finding your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)):

```powershell
$env:ASSAY_PLEX_URL = "http://192.168.1.10:32400"
$env:ASSAY_PLEX_TOKEN = "your-token"
```

## Usage

```sh
assay plex libraries                 # check the connection
assay plex scan                      # cache stream details in assay.db (incremental)
assay clients                        # list client profiles
assay check "Blade Runner" -c lg-webos
assay report -c lg-webos             # library-wide, grouped by cause
assay report -c plex-web --max-bitrate 8000   # include the client's quality setting
```

### Checking predictions against real playback

`assay plex watch` polls Plex's active sessions, records what Plex actually decided (direct play, remux, audio transcode, subtitle burn-in, video transcode) and compares it with Assay's prediction for the same file, audio track and subtitle. Where they disagree, it says which part of the profile is probably wrong.

```sh
assay plex watch                     # leave running while you play things; Ctrl+C to stop
assay history                        # recorded playbacks, prediction accuracy per profile
assay history --mismatches
assay clients                        # profiles, plus every player seen and its profile
assay clients map "Bedroom Roku" my-roku.toml
```

Players are matched to profiles automatically using `match_platforms` and `match_products` in each profile, or explicitly with `assay clients map`.

### Asking Plex directly

`assay plex ask` and `assay plex verify` use Plex's transcode decision endpoint to ask what Plex *would* do for a client, without playing anything. They show Plex's own explanation (e.g. "Audio codec dca is not supported") next to Assay's prediction, and `verify` groups the disagreements into suggested profile fixes across the library.

```sh
assay plex ask "Blade Runner" -c lg-webos
assay plex ask "Blade Runner" -c lg-webos --raw        # Plex's raw JSON, for debugging
assay plex verify -c lg-webos --sample 200             # random sample of the library
assay plex verify --as-player "Living Room TV"         # ask as a real player seen by watch
assay plex verify -c lg-webos --subtitles none --max-bitrate 8000
```

Plex chooses the client's capabilities from the product and platform it's told, set by `[plex_identity]` in each profile, or taken from a real player with `--as-player`. Real apps can advertise extra capabilities during playback, so `plex watch` remains the ground truth; `verify` is the fast way to check a whole library.

## File health

These commands read the media files themselves, so they need [ffmpeg](https://ffmpeg.org/) (`winget install Gyan.FFmpeg` on Windows; or set `ASSAY_FFPROBE` / `ASSAY_FFMPEG`) and access to the files, either on the Plex server itself or over a network share.

Plex reports paths as the server sees them. If Assay runs on another machine, map them:

```sh
assay paths                                  # where Plex's files live, and current mappings
assay paths add /media \\NAS\media            # Plex path prefix -> path from this machine
assay paths test                             # check a sample of files can be found
```

Then:

```sh
assay probe                                  # ffprobe every file (incremental)
assay deepscan --limit 50                    # read every packet of 50 more files (resumable)
assay deepscan "Blade Runner" --full         # decode every frame of one title
assay health                                 # library-wide problems, worst first
```

- `probe` finds files that are missing, unreadable, changed since Plex analysed them, or shorter than Plex thinks (usually truncated). It also fills in details Plex sometimes lacks, such as bit depth and Dolby Vision profile, which `check` and `report` then use.
- `deepscan` quick mode reads and parses every packet, which catches truncation and container damage at disk or network speed. `--full` decodes every frame and also catches corrupt video, but is roughly real-time for 4K on a CPU. Both skip files that haven't changed since their last scan, so they can be run in batches.
- `check` and `report` include any health problems found.

## Client profiles

Profiles in `src/assay/profiles/*.toml` describe what a client can direct play.
The bundled ones are hand-written starting points, **not yet verified against
real Plex decisions**. Copy one and pass its path to `-c` to customise it, and use `assay plex watch` to find where it's wrong.

## Layout

- `assay/models.py`: normalized media model and `Finding`
- `assay/plex/`: API client and Plex JSON parser
- `assay/diagnose/`: the rules engine (pure functions, no I/O)
- `assay/store.py`, `assay/scan.py`: SQLite cache and incremental scan
- `assay/watch.py`: session parsing, prediction for a specific playback, mismatch hints
- `assay/plex/decision.py`, `assay/verify.py`: Plex decision endpoint and bulk comparison
- `assay/paths.py`: Plex-to-local path mapping
- `assay/ffmpeg.py`, `assay/filescan.py`, `assay/health.py`: ffprobe/ffmpeg wrappers, incremental file scans, health findings
- `assay/cli.py`: Typer CLI

## Roadmap

- Remux-first fix queue (ffmpeg), tracking space saved
