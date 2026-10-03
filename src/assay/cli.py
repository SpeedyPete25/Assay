from __future__ import annotations

from collections import Counter, defaultdict
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
from assay.profiles import bundled_profiles, load_profile
from assay.scan import scan_library, scannable_sections
from assay.store import Store

app = typer.Typer(help="Assay: Plex library health and 'why is this transcoding?' diagnostics.", no_args_is_help=True)
plex_app = typer.Typer(help="Talk to your Plex Media Server.", no_args_is_help=True)
app.add_typer(plex_app, name="plex")

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


@app.command("clients")
def clients() -> None:
    """List bundled client profiles."""
    table = Table("Id", "Name", "Notes")
    for p in bundled_profiles().values():
        table.add_row(p.id, p.name, escape(" ".join(p.notes.split())))
    console.print(table)


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


if __name__ == "__main__":
    app()
