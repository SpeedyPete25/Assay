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
- `assay/cli.py`: Typer CLI

## Roadmap

- Use Plex's transcode decision endpoint to validate profiles
- ffprobe and deep-decode passes for corruption and details Plex doesn't expose
- Remux-first fix queue (ffmpeg), tracking space saved
