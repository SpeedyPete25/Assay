from __future__ import annotations

import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress
from rich.table import Table

from assay.diagnose import Diagnosis, diagnose
from assay.models import Impact
from assay.plex import PlexClient, PlexError
from assay.plex.decision import ClientIdentity
from assay.profiles import Profile, bundled_profiles, load_profile
from assay.scan import scan_library, scannable_sections
from assay.store import Store
from assay.verify import Check, check_version, verify
from assay.watch import Player, Result, match_profile, poll_once

app = typer.Typer(help="Assay: Plex library health and 'why is this transcoding?' diagnostics.", no_args_is_help=True)
plex_app = typer.Typer(help="Talk to your Plex Media Server.", no_args_is_help=True)
app.add_typer(plex_app, name="plex")
clients_app = typer.Typer(help="Client profiles and which Plex players use them.")
app.add_typer(clients_app, name="clients")

console = Console()

DbOption = Annotated[Path, typer.Option("--db", envvar="ASSAY_DB", help="SQLite cache file.")]
UrlOption = Annotated[str, typer.Option("--url", envvar="ASSAY_PLEX_URL", help="e.g. http://192.168.1.10:32400")]
TokenOption = Annotated[str, typer.Option("--token", envvar="ASSAY_PLEX_TOKEN", help="Your X-Plex-Token.")]
ClientOption = Annotated[str, typer.Option("--client", "-c", help="Client profile id or path to a .toml profile.")]
BitrateOption = Annotated[
    int | None,
    typer.Option("--max-bitrate", help="Client quality limit in kbps (e.g. 8000 for '8 Mbps 1080p')."),
]

IMPACT_STYLE = {
    Impact.DIRECT_PLAY: "green",
    Impact.DIRECT_STREAM: "yellow",
    Impact.TRANSCODE: "red",
}


def _impact(impact: Impact) -> str:
    return f"[{IMPACT_STYLE[impact]}]{impact.label}[/]"


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/]")
    raise typer.Exit(1)


def _profile(client: str, max_bitrate: int | None):
    try:
        return load_profile(client).with_max_bitrate(max_bitrate)
    except KeyError as e:
        _fail(str(e.args[0]))


@plex_app.command("libraries")
def plex_libraries(url: UrlOption, token: TokenOption) -> None:
    """List the server's library sections."""
    try:
        with PlexClient(url, token) as plex:
            sections = plex.sections()
    except PlexError as e:
        _fail(str(e))
    table = Table("Key", "Title", "Type")
    for s in sections:
        table.add_row(str(s["key"]), s["title"], s["type"])
    console.print(table)


@plex_app.command("scan")
def plex_scan(
    url: UrlOption,
    token: TokenOption,
    db: DbOption = Path("assay.db"),
    library: Annotated[list[str] | None, typer.Option("--library", "-l", help="Only scan these libraries.")] = None,
    workers: Annotated[int, typer.Option(help="Concurrent metadata requests.")] = 8,
) -> None:
    """Pull stream details for movie and TV libraries into the local cache."""
    try:
        with PlexClient(url, token) as plex, Store(db) as store:
            sections = scannable_sections(plex, library)
            if not sections:
                _fail("No matching movie/TV libraries found.")
            for section in sections:
                with Progress(console=console, transient=True) as progress:
                    task = progress.add_task(f"Scanning {section['title']}", total=None)
                    result = scan_library(
                        plex,
                        store,
                        section,
                        workers=workers,
                        on_progress=lambda done, total: progress.update(task, completed=done, total=total),
                    )
                console.print(
                    f"[bold]{result.library}[/]: {result.total} items, "
                    f"{result.fetched} updated, {result.removed} removed"
                )
            console.print(f"Cache now holds {store.count()} items ({db}).")
    except PlexError as e:
        _fail(str(e))


@clients_app.callback(invoke_without_command=True)
def clients(ctx: typer.Context, db: DbOption = Path("assay.db")) -> None:
    """List client profiles, and Plex players seen by 'assay plex watch'."""
    if ctx.invoked_subcommand:
        return
    profiles = bundled_profiles()
    table = Table("Id", "Name", "Notes", title="Client profiles")
    for p in profiles.values():
        table.add_row(p.id, p.name, escape(" ".join(p.notes.split())))
    console.print(table)

    with Store(db) as store:
        players = store.seen_players()
        mappings = store.client_mappings()
    if players:
        seen = Table("Player", "Product", "Platform", "Playbacks", "Profile", title="Players seen")
        for title, product, platform, count in players:
            profile = match_profile(Player(title, product, platform, ""), mappings, profiles)
            how = "" if not profile else " [dim](mapped)[/]" if title.lower() in mappings else " [dim](auto)[/]"
            seen.add_row(escape(title), product, platform, str(count), (escape(profile) + how) if profile else "[yellow]none[/]")
        console.print(seen)
        console.print("[dim]Assign a profile with: assay clients map \"<player>\" <profile>[/]")


@clients_app.command("map")
def clients_map(
    player: Annotated[str, typer.Argument(help="Plex player name, as shown by 'assay clients'.")],
    profile: Annotated[str, typer.Argument(help="Profile id or path to a .toml profile.")],
    db: DbOption = Path("assay.db"),
) -> None:
    """Use a profile for a Plex player when watching."""
    resolved = _profile(profile, None)
    if Path(profile).suffix == ".toml":
        profile = str(Path(profile).resolve())
    with Store(db) as store:
        store.map_client(player, profile)
    console.print(f"'{escape(player)}' now uses [bold]{escape(resolved.name)}[/].")


@clients_app.command("unmap")
def clients_unmap(player: str, db: DbOption = Path("assay.db")) -> None:
    """Remove a player mapping (falls back to automatic matching)."""
    with Store(db) as store:
        removed = store.unmap_client(player)
    console.print(f"Removed mapping for '{escape(player)}'." if removed else f"No mapping for '{escape(player)}'.")


def _print_diagnosis(d: Diagnosis) -> None:
    v = d.version
    header = f"[bold]{d.item.title}[/]"
    if len(d.item.versions) > 1:
        header += f"  [dim](version {v.id})[/]"
    console.print(header)
    for f in v.files:
        console.print(f"  [dim]{f}[/]")

    verdict = f"  On [bold]{d.profile.name}[/]: {_impact(d.impact)}"
    if d.worst_case != d.impact:
        verdict += f"  (worst case: {_impact(d.worst_case)})"
    console.print(verdict)

    if not d.findings:
        console.print("  [green]No problems found.[/]")
    for f in d.findings:
        tag = _impact(f.impact) if f.impact is not None else "[magenta]Health[/]" if f.category == "health" else "[cyan]Note[/]"
        console.print(f"  - {tag} {f.message}")
        if f.condition:
            console.print(f"      [dim]Applies {f.condition}.[/]")
        if f.fix:
            console.print(f"      [dim]Fix:[/] {f.fix}")
    console.print()


@app.command("check")
def check(
    query: Annotated[str, typer.Argument(help="Title to search for, or a Plex ratingKey.")],
    client: ClientOption,
    db: DbOption = Path("assay.db"),
    max_bitrate: BitrateOption = None,
) -> None:
    """Explain how one title will play on a client, and why."""
    profile = _profile(client, max_bitrate)
    with Store(db) as store:
        item = store.get(query)
        items = [item] if item else store.search(query)
    if not items:
        _fail(f"Nothing in the cache matches '{query}'. Run 'assay plex scan' first.")
    if len(items) > 5:
        console.print(f"[yellow]{len(items)} matches; showing the first 5. Narrow the search or use a ratingKey.[/]\n")
    for item in items[:5]:
        for d in diagnose(item, profile):
            _print_diagnosis(d)


@app.command("report")
def report(
    client: ClientOption,
    db: DbOption = Path("assay.db"),
    library: Annotated[str | None, typer.Option("--library", "-l")] = None,
    max_bitrate: BitrateOption = None,
) -> None:
    """Library-wide summary for a client: what won't direct play, grouped by cause."""
    profile = _profile(client, max_bitrate)
    verdicts: Counter[Impact] = Counter()
    worst: Counter[Impact] = Counter()
    by_code: dict[str, dict] = defaultdict(dict)  # code -> {version id: (Finding, Diagnosis)}, one entry per file
    total = 0

    with Store(db) as store:
        for item in store.items(library):
            for d in diagnose(item, profile):
                total += 1
                verdicts[d.impact] += 1
                worst[d.worst_case] += 1
                for f in d.findings:
                    by_code[f.code].setdefault(d.version.id, (f, d))

    if not total:
        _fail("The cache is empty. Run 'assay plex scan' first.")

    console.print(f"[bold]{total} files on {profile.name}[/]\n")
    summary = Table("Decision", "Default settings", "Worst case")
    for impact in Impact:
        summary.add_row(_impact(impact), str(verdicts[impact]), str(worst[impact]))
    console.print(summary)

    causes = Table("Files", "Effect", "Cause", "Example", title="Causes, most common first")
    for code, hits in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
        finding, example = next(iter(hits.values()))
        effect = _impact(finding.impact) if finding.impact is not None else "Health" if finding.category == "health" else "Note"
        if finding.condition:
            effect += " [dim](conditional)[/]"
        causes.add_row(str(len(hits)), effect, code, example.item.title)
    console.print(causes)
    console.print("[dim]Use 'assay check <title> -c <client>' for details and fixes.[/]")


def _verdict_mark(matched: bool | None) -> str:
    return {True: "[green]match[/]", False: "[red]MISMATCH[/]", None: "[dim]no profile[/]"}[matched]


def _print_result(r: Result) -> None:
    obs = r.obs
    stamp = datetime.now().strftime("%H:%M:%S")
    decisions = f"video {obs.video_decision}, audio {obs.audio_decision or 'directplay'}"
    if obs.subtitle_decision:
        decisions += f", subs {obs.subtitle_decision}"
    console.print(f"[dim]{stamp}[/] [bold]{escape(obs.title)}[/] on {escape(obs.player.title)} ({obs.player.product}, {obs.player.platform})")
    line = f"  Plex: {_impact(obs.actual)} [dim]({decisions})[/]"
    if r.diagnosis:
        line += f"   Assay ({r.profile_id}): {_impact(r.diagnosis.impact)}   {_verdict_mark(r.matched)}"
    else:
        line += f"   {_verdict_mark(None)}: run 'assay clients map \"{escape(obs.player.title)}\" <profile>'"
    console.print(line)
    for hint in r.hints:
        console.print(f"  [yellow]-[/] {escape(hint)}")


@plex_app.command("watch")
def plex_watch(
    url: UrlOption,
    token: TokenOption,
    db: DbOption = Path("assay.db"),
    interval: Annotated[int, typer.Option(help="Seconds between polls.")] = 10,
    once: Annotated[bool, typer.Option("--once", help="Poll a single time and exit.")] = False,
) -> None:
    """Record Plex's real playback decisions and compare them with Assay's predictions."""
    with PlexClient(url, token) as plex, Store(db) as store:
        if not once:
            console.print(f"Watching Plex sessions every {interval}s. Press Ctrl+C to stop.\n")
        try:
            while True:
                try:
                    results = poll_once(plex, store)
                except PlexError as e:
                    if once:
                        _fail(str(e))
                    console.print(f"[yellow]{e} (retrying)[/]")
                    results = []
                for r in results:
                    if r.new:
                        _print_result(r)
                if once:
                    if not results:
                        console.print("No video is playing right now.")
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            console.print("Stopped.")


@app.command("history")
def history(
    db: DbOption = Path("assay.db"),
    limit: Annotated[int, typer.Option("--limit", "-n")] = 25,
    mismatches: Annotated[bool, typer.Option("--mismatches", help="Only show wrong predictions.")] = False,
) -> None:
    """Recorded playbacks: Plex's decision vs Assay's prediction."""
    with Store(db) as store:
        playbacks = store.playbacks(limit, mismatches)
        everything = store.playbacks(limit=1_000_000)
    if not playbacks:
        _fail("No playbacks recorded. Run 'assay plex watch' while something plays.")

    table = Table("When", "Title", "Player", "Plex", "Assay", "", "Notes")
    for p in playbacks:
        table.add_row(
            datetime.fromtimestamp(p.observed_at).strftime("%m-%d %H:%M"),
            escape(p.title),
            escape(p.player_title),
            _impact(p.actual),
            f"{_impact(p.predicted)} [dim]{p.profile_id}[/]" if p.predicted is not None else "",
            _verdict_mark(p.matched),
            escape("\n".join(p.hints)),
        )
    console.print(table)

    by_profile: dict[str, Counter] = defaultdict(Counter)
    for p in everything:
        if p.matched is not None:
            by_profile[p.profile_id][p.matched] += 1
    for profile_id, counts in sorted(by_profile.items()):
        total = counts[True] + counts[False]
        console.print(f"[bold]{profile_id}[/]: {counts[True]}/{total} predictions correct")


class SubtitleMode(str, Enum):
    auto = "auto"
    none = "none"


SubtitlesOption = Annotated[
    SubtitleMode,
    typer.Option("--subtitles", help="'auto' uses your account's subtitle choice per title; 'none' turns them off."),
]
AsPlayerOption = Annotated[
    str | None,
    typer.Option("--as-player", help="Ask as a player seen by 'assay plex watch' (uses its real product/platform)."),
]
OptionalClientOption = Annotated[
    str | None, typer.Option("--client", "-c", help="Client profile id or .toml path (optional with --as-player).")
]


def _target(store: Store, client: str | None, as_player: str | None, max_bitrate: int | None) -> tuple[Profile, ClientIdentity]:
    """Work out which profile to predict with and which identity to ask Plex as."""
    identity = None
    if as_player:
        player = next((p for p in store.seen_players() if p[0].lower() == as_player.lower()), None)
        if player is None:
            _fail(f"No player named '{as_player}' has been seen. Run 'assay plex watch' while it plays, or see 'assay clients'.")
        title, product, platform, _ = player
        identity = ClientIdentity(product=product, platform=platform)
        if client is None:
            client = match_profile(Player(title, product, platform, ""), store.client_mappings(), bundled_profiles())
            if client is None:
                _fail(f"'{as_player}' has no profile. Pass -c, or map one with 'assay clients map'.")
    if client is None:
        _fail("Pass --client/-c, or --as-player.")

    profile = _profile(client, max_bitrate)
    identity = identity or profile.plex_identity
    if identity is None:
        _fail(f"Profile '{profile.id}' has no [plex_identity] section. Add one, or use --as-player.")
    return profile, identity


def _find_items(store: Store, query: str, limit: int = 5):
    item = store.get(query)
    items = [item] if item else store.search(query)
    if not items:
        _fail(f"Nothing in the cache matches '{query}'. Run 'assay plex scan' first.")
    if len(items) > limit:
        console.print(f"[yellow]{len(items)} matches; showing the first {limit}. Narrow the search or use a ratingKey.[/]\n")
    return items[:limit]


def _print_check(c: Check, identity: ClientIdentity) -> None:
    header = f"[bold]{escape(c.item.title)}[/]"
    if len(c.item.versions) > 1:
        header += f"  [dim](version {c.version.id})[/]"
    console.print(header)
    console.print(f"  [dim]Asked Plex as {escape(identity.product)} ({escape(identity.platform)})[/]")
    if c.error:
        console.print(f"  [red]{escape(c.error)}[/]\n")
        return
    d = c.decision
    streams = ", ".join(
        f"{kind} {value}"
        for kind, value in (("video", d.video_decision), ("audio", d.audio_decision), ("subs", d.subtitle_decision))
        if value
    )
    console.print(f"  Plex:  {_impact(d.impact)}" + (f" [dim]({streams})[/]" if streams else ""))
    if d.reason:
        console.print(f'         [dim]"{escape(d.reason)}"[/]')
    console.print(f"  Assay: {_impact(c.diagnosis.impact)} [dim]({c.diagnosis.profile.id})[/]   {_verdict_mark(c.matched)}")
    for hint in c.hints:
        console.print(f"  [yellow]-[/] {escape(hint)}")
    console.print()


@plex_app.command("ask")
def plex_ask(
    query: Annotated[str, typer.Argument(help="Title to search for, or a Plex ratingKey.")],
    url: UrlOption,
    token: TokenOption,
    client: OptionalClientOption = None,
    as_player: AsPlayerOption = None,
    db: DbOption = Path("assay.db"),
    subtitles: SubtitlesOption = SubtitleMode.auto,
    max_bitrate: BitrateOption = None,
    raw: Annotated[bool, typer.Option("--raw", help="Print Plex's raw decision JSON.")] = False,
) -> None:
    """Ask Plex how it would play a title on a client, and compare with Assay."""
    with PlexClient(url, token) as plex, Store(db) as store:
        profile, identity = _target(store, client, as_player, max_bitrate)
        for item in _find_items(store, query):
            for i in range(len(item.versions)):
                c = check_version(plex, item, i, profile, identity, subtitles=subtitles.value)
                if raw and c.decision:
                    console.print_json(data=c.decision.raw)
                else:
                    _print_check(c, identity)


@plex_app.command("verify")
def plex_verify(
    url: UrlOption,
    token: TokenOption,
    client: OptionalClientOption = None,
    as_player: AsPlayerOption = None,
    db: DbOption = Path("assay.db"),
    library: Annotated[str | None, typer.Option("--library", "-l")] = None,
    sample: Annotated[int | None, typer.Option(help="Check a random sample of this many titles.")] = None,
    subtitles: SubtitlesOption = SubtitleMode.auto,
    max_bitrate: BitrateOption = None,
    workers: Annotated[int, typer.Option(help="Concurrent decision requests.")] = 4,
) -> None:
    """Check a profile against Plex's own decisions across the library, without playing anything."""
    with PlexClient(url, token) as plex, Store(db) as store:
        profile, identity = _target(store, client, as_player, max_bitrate)
        items = [i for i in store.items(library) if i.versions]
        if not items:
            _fail("The cache is empty. Run 'assay plex scan' first.")
        if sample and sample < len(items):
            items = random.sample(items, sample)

        # One request up front, so a rejected request fails fast instead of once per title.
        first = check_version(plex, items[0], 0, profile, identity, subtitles=subtitles.value)
        if first.error:
            _fail(f"Plex rejected the decision request: {first.error}\nTry: assay plex ask {items[0].rating_key} -c {profile.id} --raw")

        with Progress(console=console, transient=True) as progress:
            task = progress.add_task(f"Asking Plex as {identity.product}", total=None)
            checks = verify(
                plex,
                items,
                profile,
                identity,
                subtitles=subtitles.value,
                workers=workers,
                on_progress=lambda done, total: progress.update(task, completed=done, total=total),
            )

    compared = [c for c in checks if c.matched is not None]
    errors = [c for c in checks if c.error]
    agree = sum(c.matched for c in compared)
    console.print(
        f"[bold]{len(compared)} files[/] checked as {escape(identity.product)} ({escape(identity.platform)}) "
        f"against profile [bold]{profile.id}[/]: {agree}/{len(compared)} predictions match "
        f"({100 * agree / max(len(compared), 1):.0f}%)\n"
    )

    matrix = Table("Plex \\ Assay", *(i.label for i in Impact), title="Plex's decision vs Assay's prediction")
    counts = Counter((c.decision.impact, c.diagnosis.impact) for c in compared)
    for actual in Impact:
        matrix.add_row(_impact(actual), *(str(counts[actual, predicted]) for predicted in Impact))
    console.print(matrix)

    by_hint: dict[str, list[Check]] = defaultdict(list)
    for c in compared:
        for hint in c.hints:
            by_hint[hint].append(c)
    if by_hint:
        fixes = Table("Files", "Suggestion", "Example", title="Suggested profile fixes")
        for hint, hits in sorted(by_hint.items(), key=lambda kv: -len(kv[1])):
            fixes.add_row(str(len(hits)), escape(hint), escape(hits[0].item.title))
        console.print(fixes)

    mismatches = [c for c in compared if not c.matched]
    if mismatches:
        table = Table("Title", "Plex", "Assay", "Plex's reason", title=f"Mismatches (first 15 of {len(mismatches)})")
        for c in mismatches[:15]:
            table.add_row(escape(c.item.title), _impact(c.decision.impact), _impact(c.diagnosis.impact), escape(c.decision.reason))
        console.print(table)

    if errors:
        console.print(f"[yellow]{len(errors)} requests failed, e.g. {escape(errors[0].item.title)}: {escape(errors[0].error)}[/]")
    console.print("[dim]Plex answers from its server-side profile for this identity; the real app may support more.[/]")


if __name__ == "__main__":
    app()
