"""Typer CLI entry point for ``clipforage``."""

import json
import platform
import re
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any, TextIO, get_args

import httpx
import typer

from clipforage import (
    __version__,
    assist,
    config,
    recall,
    recall_online,
    rights,
    safesearch,
    service,
)
from clipforage.assist import claude_cli
from clipforage.cache import Cache, CaptionIndexUnavailableError
from clipforage.download import binaries
from clipforage.download.binaries import SetupError
from clipforage.download.service import FINAL_FRACTION_KEY, DownloadError, parse_clip
from clipforage.editors import resolve as resolve_editor
from clipforage.linkout import linkouts_for
from clipforage.models import Asset, DownloadResult, Kind, PhraseHit, SearchFilters, Style
from clipforage.providers.base import ProviderError

app = typer.Typer(
    help="Find and download editing resources with license info.", no_args_is_help=True
)

TOOLS: tuple[str, ...] = ("ffmpeg", "deno", "yt-dlp")
SETUP_TOOLS: tuple[str, ...] = ("ffmpeg", "deno")
ORIENTATIONS: tuple[str, ...] = ("landscape", "portrait", "square")
STYLES: tuple[str, ...] = get_args(Style)
LINE_WIDTH = 100
_PROBE = "👻"  # an emoji: not encodable in cp1252 and other legacy code pages


def _make_encodable(stream: TextIO | None) -> None:
    """Make ``stream`` replace unencodable characters instead of raising (e.g. cp1252)."""
    encoding = getattr(stream, "encoding", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if not encoding or reconfigure is None:
        return
    try:
        _PROBE.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        try:
            reconfigure(errors="replace")
        except (AttributeError, OSError, TypeError, ValueError):
            pass  # not reconfigurable; leave it as is


@app.callback()
def _main() -> None:
    """Find and download editing resources with license info."""
    _make_encodable(sys.stdout)
    _make_encodable(sys.stderr)
    for message in config.migrate_legacy_dirs():  # no-op unless pre-rename dirs exist
        typer.echo(message, err=True)


@app.command()
def version() -> None:
    """Print the clipforage version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Report ffmpeg / Deno (data dir, then PATH) and the yt-dlp version. Always exits 0."""
    for tool in TOOLS:
        if tool == "yt-dlp":
            typer.echo(f"{tool:<8} {binaries.ytdlp_version()}")
            continue
        path = binaries.find_tool(tool)
        status = f"found    {path}" if path else "missing  (run: clipforage setup)"
        typer.echo(f"{tool:<8} {status}")
    if binaries.has_ejs():
        typer.echo("yt-dlp-ejs  found")
    else:
        typer.echo('yt-dlp-ejs  missing  (run: uv sync, or pip install "yt-dlp[default]")')
    claude = claude_cli.find_claude()
    if claude is None:
        typer.echo("claude   missing  (optional, for `describe`: https://claude.com/claude-code)")
    else:
        ver = claude_cli.version(claude) or "version unknown"
        typer.echo(f"claude   found    {claude} (Claude Code {ver})")


@app.command()
def setup(
    tool: Annotated[
        str | None, typer.Option("--tool", help="Only install this tool: ffmpeg or deno.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Reinstall even if present.")] = False,
) -> None:
    """Download official ffmpeg and Deno releases into the user data dir."""
    if tool is not None and tool not in SETUP_TOOLS:
        valid = ", ".join(SETUP_TOOLS)
        typer.echo(f"error: invalid tool {tool!r}; choose one of: {valid}", err=True)
        raise typer.Exit(code=1)
    releases = binaries.releases_for()
    failed = False
    for name in [tool] if tool else SETUP_TOOLS:
        release = releases.get(name)
        if release is None:
            if name == "ffmpeg" and platform.system() == "Darwin":
                typer.echo(binaries.MACOS_FFMPEG_HINT)
            else:
                typer.echo(f"error: no verified official {name} build for this platform", err=True)
                failed = True
            continue
        existed = (binaries.bin_dir() / binaries.exe(name)).is_file() and not force
        last = [""]

        def show(
            done: int, total: int | None, asset: str = release.name, last: list[str] = last
        ) -> None:
            size = f"{done / 1e6:.1f}/{total / 1e6:.1f} MB" if total else f"{done / 1e6:.1f} MB"
            line = f"\rdownloading {asset} {size}"
            if line != last[0]:
                last[0] = line
                typer.echo(line, nl=False)

        try:
            path = binaries.install(name, echo=typer.echo, force=force, progress=show)
        except SetupError as exc:
            if last[0]:
                typer.echo("")
            typer.echo(f"error: {exc}", err=True)
            failed = True
            continue
        if last[0]:
            typer.echo("")
        if not existed:
            typer.echo(f"installed {name} -> {path}")
    if failed:
        raise typer.Exit(code=1)


def _print_progress(d: dict[str, Any]) -> None:
    """yt-dlp progress hook: one ``\\r``-updated line, newline when the download finishes.

    Only clipforage's final ``finished`` event (it carries ``fraction``) prints ``done``;
    yt-dlp's own per-file ``finished`` events (no ``fraction``) print nothing.
    """
    status = d.get("status")
    if status == "downloading":
        fraction = d.get("fraction")
        if d.get("downloaded_bytes") is None and isinstance(fraction, int | float):
            typer.echo(f"\rclipping {round(fraction * 100)}%", nl=False)  # ffmpeg http clip
            return
        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            typer.echo(f"\rdownloading {done / total * 100:5.1f}%", nl=False)
        else:
            typer.echo(f"\rdownloading {done / 1_048_576:.1f} MiB", nl=False)
    elif status == "postprocessing":
        typer.echo(f"\rprocessing ({d.get('postprocessor')})...", nl=False)
    elif status == "finished" and FINAL_FRACTION_KEY in d:
        typer.echo("\rdownloading done  ")


@app.command()
def get(
    url: Annotated[
        str,
        typer.Argument(help="Any URL yt-dlp supports, or <provider>:<id> from search output."),
    ],
    mp3: Annotated[bool, typer.Option("--mp3", help="Extract audio as mp3.")] = False,
    clip: Annotated[
        str | None,
        typer.Option(
            "--clip",
            help="Only this range, e.g. 00:05-00:12 or 10-25. Also works for Internet Archive "
            "and stock video/audio refs (re-encoded with ffmpeg to mp4 / mp3).",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output folder (default: [library] dir in config.toml, else "
            "~/Downloads/clipforage).",
        ),
    ] = None,
    cookies_from_browser: Annotated[
        str | None,
        typer.Option(
            "--cookies-from-browser",
            help="Use this browser's cookies, e.g. firefox, chrome, edge "
            "(BROWSER[+KEYRING][:PROFILE][::CONTAINER]).",
        ),
    ] = None,
    max_wall_s: Annotated[
        float | None,
        typer.Option(
            "--max-wall-s",
            help="Wall-clock limit in seconds for HTTP clip re-encodes; 0 disables. "
            r"Default 300 or \[download] clip_max_wall_s.",  # \[ keeps Rich from eating [download]
        ),
    ] = None,
    quality: Annotated[
        str | None,
        typer.Option(
            "--quality",
            "-q",
            help="Max video height for yt-dlp downloads: best, 2160, 1440, 1080, 720, 480, 360. "
            r"Default \[download] quality, else best. Ignored with --mp3 and for stock files.",
        ),
    ] = None,
) -> None:
    """Download a URL or a cached ``<provider>:<id>`` as mp4 (default) or mp3, with a sidecar."""
    out = out or config.library_dir()
    if not url.startswith(("http://", "https://")):
        url, result = _get_ref(url, mp3, clip, out, cookies_from_browser, max_wall_s, quality)
    else:
        try:
            clip_range = parse_clip(clip) if clip else None
            result = service.download_link(
                url,
                out=out,
                mp3=mp3,
                clip=clip_range,
                progress=_print_progress,
                cookies_from_browser=cookies_from_browser,
                quality=quality,
            )
        except (DownloadError, ValueError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from None
    meta = json.loads(result.sidecar.read_text(encoding="utf-8"))
    typer.echo(f"saved    {result.path}")
    typer.echo(f"sidecar  {result.sidecar}")
    typer.echo(f"license  {meta.get('license', 'unknown')}")
    typer.echo(meta.get("attribution_text", ""))
    service.record_history(url, result, warn=_warn)


def _warn(message: str) -> None:
    typer.echo(f"warning: {message}", err=True)


def _get_ref(
    ref: str,
    mp3: bool,
    clip: str | None,
    out: Path | None,
    cookies_from_browser: str | None,
    max_wall_s: float | None = None,
    quality: str | None = None,
) -> tuple[str, DownloadResult]:
    """Map ``service.download_ref`` errors to ``error: ...`` + exit 1."""
    try:
        service.resolve_ref(ref)  # report ref / cache errors before a bad --clip
        clip_range = parse_clip(clip) if clip else None
        return service.download_ref(
            ref,
            out=out,
            mp3=mp3,
            clip=clip_range,
            progress=_print_progress,
            cookies_from_browser=cookies_from_browser,
            max_wall_s=max_wall_s,
            quality=quality,
        )
    except (
        service.CacheUnavailableError,
        DownloadError,
        ProviderError,
        httpx.HTTPError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max rows shown.")] = 20,
) -> None:
    """List recent downloads, newest first."""
    try:
        rows = service.history(limit)
    except service.CacheUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not rows:
        typer.echo("no downloads yet")
    for row in rows:
        typer.echo(
            f"#{row['id']}  {row['fetched_at']}  {row['provider']}  {row['title']}  ->  {row['path']}"
        )


def _print_keyout_progress(d: dict[str, Any]) -> None:
    """``key_out`` progress hook: one ``\r``-updated ``keying N%`` line, newline when done."""
    status = d.get("status")
    fraction = d.get("fraction")
    if status == "downloading" and isinstance(fraction, int | float):
        typer.echo(f"\rkeying {round(fraction * 100)}%", nl=False)
    elif status == "finished":
        typer.echo("\rkeying done  ")


@app.command()
def keyout(
    ref: Annotated[
        str,
        typer.Argument(help="A download id from `clipforage history` (e.g. 12) or a video file."),
    ],
    tolerance: Annotated[
        int,
        typer.Option(
            "--tolerance",
            "-t",
            min=0,
            max=120,
            help="How much green to key out, 0-120 (higher keys more).",
        ),
    ] = 80,
    max_wall_s: Annotated[
        float | None,
        typer.Option(
            "--max-wall-s",
            help="Wall-clock limit in seconds for the ffmpeg re-encode; 0 disables. "
            r"Default 300 or \[download] clip_max_wall_s.",
        ),
    ] = None,
) -> None:
    """Key out a green screen into a transparent VP9 WebM (<name>.key.webm) beside the video."""
    try:
        if ref.isdigit():
            result = service.key_out_download(
                int(ref),
                tolerance=tolerance,
                progress=_print_keyout_progress,
                max_wall_s=max_wall_s,
                warn=_warn,
            )
        else:
            result = service.key_out_path(
                Path(ref),
                tolerance=tolerance,
                progress=_print_keyout_progress,
                max_wall_s=max_wall_s,
            )
    except (service.CacheUnavailableError, DownloadError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"saved    {result['path']}")
    if result.get("sidecar"):
        typer.echo(f"sidecar  {result['sidecar']}")


@app.command("cache-clear")
def cache_clear() -> None:
    """Delete all cached search results and indexed captions (download history is kept)."""
    captions: int | None = None
    try:
        with Cache() as cache:
            count = cache.clear_search()
            try:
                captions = cache.clear_captions()
            except CaptionIndexUnavailableError:
                captions = None  # no FTS5: there is no caption index to clear
    except (sqlite3.Error, OSError) as exc:
        typer.echo(f"error: cache unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"cleared {count} cached searches")
    if captions is not None:
        typer.echo(f"cleared captions of {captions} videos")


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _fmt_license(asset: Asset) -> str:
    if asset.license == "unknown" and asset.provider == "youtube":
        return "unknown (third-party)"
    return asset.license


_MISSING_KEY = re.compile(r"\b(?:missing|no)\s+api\s+key\b", re.IGNORECASE)


def _echo_search_errors(errors: list[ProviderError]) -> None:
    """Print provider errors; a missing API key is a one-line hint, once per provider."""
    hinted: set[str] = set()
    for err in errors:
        if _MISSING_KEY.search(err.message):
            if err.provider not in hinted:
                hinted.add(err.provider)
                env = f"CLIPFORAGE_{err.provider.upper()}_KEY"
                typer.echo(f"{err.provider} skipped: no API key (set {env})", err=True)
            continue
        typer.echo(f"warning: {err}", err=True)


def _echo_assets(assets: list[Asset], *, start: int = 1) -> None:
    """Print numbered result rows: provider, duration, license, rights level, title, ref, URL."""
    for i, asset in enumerate(assets, start=start):
        level = f"[{rights.classify(asset).level.value}]"
        prefix = (
            f"{i:>3}  {asset.provider:<10} {_fmt_duration(asset.duration_s):>6}  "
            f"{_fmt_license(asset):<22} {level:<15} "
        )
        room = max(LINE_WIDTH - len(prefix), 10)
        title = asset.title if len(asset.title) <= room else asset.title[: room - 3] + "..."
        typer.echo(prefix + title)
        typer.echo(f"     {asset.provider}:{asset.id}  {asset.source_url}")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="What to search for.")],
    kind: Annotated[str, typer.Option("--kind", help="Asset kind, e.g. video.")] = "video",
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max results shown.")] = 10,
    commercial_only: Annotated[
        bool, typer.Option("--commercial-only", help="Only results known to allow commercial use.")
    ] = False,
    min_duration: Annotated[
        float | None, typer.Option("--min-duration", help="Minimum duration in seconds.")
    ] = None,
    max_duration: Annotated[
        float | None, typer.Option("--max-duration", help="Maximum duration in seconds.")
    ] = None,
    orientation: Annotated[
        str | None, typer.Option("--orientation", help="landscape, portrait or square.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Only search this provider id.")
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Skip cached results (fresh results are still stored)."),
    ] = False,
    style: Annotated[
        str,
        typer.Option(
            "--style",
            help="Editing style: any, meme, green_screen or cinematic (refines youtube "
            "queries and caps duration).",
        ),
    ] = "any",
    no_expand: Annotated[
        bool,
        typer.Option("--no-expand", help="Do not also search offline recall expansions."),
    ] = False,
    no_related: Annotated[
        bool,
        typer.Option("--no-related", help="For audio kinds, do not search the other audio kind."),
    ] = False,
    cleared_only: Annotated[
        bool,
        typer.Option(
            "--cleared-only",
            help="Only results whose rights level is clear or attribution (drops "
            "non-commercial, copyrighted and unknown).",
        ),
    ] = False,
    unsafe: Annotated[
        bool,
        typer.Option(
            "--unsafe",
            help="Turn safe search off for this run (default on; [search] safe in config.toml "
            "or CLIPFORAGE_SAFE_SEARCH).",
        ),
    ] = False,
    shorts: Annotated[
        bool,
        typer.Option(
            "--shorts",
            help="YouTube Shorts: <= 60 s, YouTube first, portrait stock after (best effort)",
        ),
    ] = False,
) -> None:
    """Search all providers and list results with their license and rights level."""
    try:
        asset_kind = Kind(kind)
    except ValueError:
        valid = ", ".join(k.value for k in Kind)
        typer.echo(f"error: invalid kind {kind!r}; choose one of: {valid}", err=True)
        raise typer.Exit(code=1) from None
    if orientation is not None and orientation not in ORIENTATIONS:
        valid = ", ".join(ORIENTATIONS)
        typer.echo(f"error: invalid orientation {orientation!r}; choose one of: {valid}", err=True)
        raise typer.Exit(code=1)
    if style not in STYLES:
        valid = ", ".join(STYLES)
        typer.echo(f"error: invalid style {style!r}; choose one of: {valid}", err=True)
        raise typer.Exit(code=1)
    filters = SearchFilters(
        kind=asset_kind,
        commercial_only=commercial_only,
        min_duration=min_duration,
        max_duration=max_duration,
        orientation=orientation,  # type: ignore[arg-type]
        style=style,  # type: ignore[arg-type]
        expand=not no_expand,
        related=not no_related,
        safe=False if unsafe else safesearch.configured_safe(),
        shorts=shorts,
    )
    try:
        outcome = service.search_expanded(
            query,
            filters,
            provider=provider,
            use_cache=not no_cache,
            limit=limit,
            warn=_warn,
            cleared_only=cleared_only,
        )
    except LookupError as exc:
        typer.echo(f"error: {exc.args[0]}", err=True)
        raise typer.Exit(code=1) from None
    if outcome.relaxed_query:
        typer.echo(f'no cleared results for "{query}"; showing "{outcome.relaxed_query}"')
    if outcome.expanded_queries:
        typer.echo(f"also searched: {', '.join(outcome.expanded_queries)}")
    if not outcome.assets:
        typer.echo("no results")
    _echo_assets(outcome.assets)
    if outcome.related and outcome.related_kind is not None:
        typer.echo(f"related ({outcome.related_kind.value}):")
        _echo_assets(outcome.related, start=len(outcome.assets) + 1)
    if outcome.filtered_count:
        typer.echo(f"{outcome.filtered_count} result(s) hidden by safe search (--unsafe to show)")
    _echo_search_errors(outcome.errors)
    links = linkouts_for(asset_kind)
    if links:
        names = ", ".join(link.name for link in links)
        typer.echo(f"Also try (opens in browser, not searched): {names}")
    if recall_online.prefetch_running():  # a daemon thread: it dies with this process
        typer.echo(
            "note: Know Your Meme index not built yet; run "
            f'`clipforage recall --online "{query}"` once (about 40 s) to enable meme-name '
            "suggestions",
            err=True,
        )


@app.command("recall")
def recall_command(
    description: Annotated[
        str, typer.Argument(help='What the meme looks or sounds like, e.g. "dog in burning room".')
    ],
    kind: Annotated[
        str | None, typer.Option("--kind", help="Only suggest memes of this asset kind.")
    ] = None,
    max_results: Annotated[int, typer.Option("--max", min=1, help="Max suggestions printed.")] = 5,
    online: Annotated[
        bool,
        typer.Option(
            "--online",
            help="Also look the description up on Know Your Meme (throttled, cached 7 days).",
        ),
    ] = False,
    unsafe: Annotated[
        bool,
        typer.Option(
            "--unsafe",
            help="Turn safe search off for this run: --online also suggests Know Your Meme "
            "entries filed as sensitive (default on; [search] safe in config.toml or "
            "CLIPFORAGE_SAFE_SEARCH).",
        ),
    ] = False,
) -> None:
    """Suggest known meme names for a description (offline unless --online)."""
    asset_kind: Kind | None = None
    if kind is not None:
        try:
            asset_kind = Kind(kind)
        except ValueError:
            valid = ", ".join(k.value for k in Kind)
            typer.echo(f"error: invalid kind {kind!r}; choose one of: {valid}", err=True)
            raise typer.Exit(code=1) from None
    names = recall.expand_query(description, asset_kind, max_expansions=max_results)
    hits: list[recall_online.RecallHit] = []
    if online:
        try:
            hits = service.online_recall(
                description,
                asset_kind,
                safe=False if unsafe else safesearch.configured_safe(),
                block_sitemap=True,
                on_sitemap_fetch=lambda: typer.echo(
                    "building Know Your Meme index (first run, about 40 s)", err=True
                ),
            )
        except ProviderError as exc:
            typer.echo(f"warning: online lookup skipped: {exc}", err=True)
    if not names and not hits:
        typer.echo("no suggestions")
    for name in names:
        typer.echo(name)
    for hit in hits:
        also = f" (also: {', '.join(hit.aliases)})" if hit.aliases else ""
        typer.echo(f"{hit.name}{also}  [Know Your Meme: {hit.source_url}]")


def _fmt_ts(seconds: float) -> str:
    """Seconds -> ``M:SS`` (``H:MM:SS`` from one hour), rounded down."""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _hit_url(hit: PhraseHit) -> str:
    """The hit's source URL; YouTube links jump to the cue (``&t=<s>s``)."""
    if hit.asset.provider == "youtube":
        sep = "&" if "?" in hit.asset.source_url else "?"
        return f"{hit.asset.source_url}{sep}t={int(hit.start_s)}s"
    return hit.asset.source_url


@app.command()
def phrase(
    query: Annotated[str, typer.Argument(help="Spoken phrase to find in captions.")],
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="Caption source: youtube or internet_archive (repeatable; default: both).",
        ),
    ] = None,
    within: Annotated[
        str | None,
        typer.Option(
            "--within",
            help="youtube: playlist / channel / video URL to search instead; internet_archive: "
            "film identifier or title to index.",
        ),
    ] = None,
    max_videos: Annotated[
        int, typer.Option("--max-videos", min=1, help="Videos per source to index captions of.")
    ] = 10,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max hits shown.")] = 20,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Refetch captions even if already indexed.")
    ] = False,
    max_per_video: Annotated[
        int, typer.Option("--max-per-video", min=0, help="Max hits per video (0 = unlimited).")
    ] = 3,
    unsafe: Annotated[
        bool,
        typer.Option(
            "--unsafe",
            help="Turn safe search off for this run (default on; [search] safe in config.toml "
            "or CLIPFORAGE_SAFE_SEARCH).",
        ),
    ] = False,
) -> None:
    """Find a spoken phrase in YouTube captions and Internet Archive subtitles, with timestamps.

    Internet Archive finds films by title (or --within); quotes match inside films already
    indexed. Every run adds its videos to the local caption index, which is searched as a whole.
    """
    try:
        outcome = service.phrase_search(
            query,
            sources=source or None,
            within=within,
            max_videos=max_videos,
            limit=limit,
            use_cache=not no_cache,
            warn=_warn,
            max_per_video=max_per_video,
            safe=False if unsafe else None,
        )
    except LookupError as exc:
        typer.echo(f"error: {exc.args[0]}", err=True)
        raise typer.Exit(code=1) from None
    except service.CacheUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    hits = outcome.hits
    if not hits:
        typer.echo("no hits")
    for i, hit in enumerate(hits, start=1):
        span = f"{_fmt_ts(hit.start_s)}-{_fmt_ts(hit.end_s)}"
        prefix = f"{i:>3}  {hit.asset.provider:<11} {span:>9}  "
        room = max(LINE_WIDTH - len(prefix), 10)
        title = hit.asset.title
        title = title if len(title) <= room else title[: room - 3] + "..."
        typer.echo(prefix + title)
        typer.echo(f'     {hit.ref}  {_hit_url(hit)}  "{hit.text}"')
        typer.echo(f"     get {hit.ref} --clip {hit.clip_spec()}")
    if outcome.filtered_count:
        typer.echo(f"{outcome.filtered_count} result(s) hidden by safe search (--unsafe to show)")
    for err in outcome.errors:
        typer.echo(f"warning: {err}", err=True)
    quote_links = [link.name for link in linkouts_for(None) if link.name == "PlayPhrase"]
    if quote_links:
        typer.echo(f"Quote lookup (opens in browser, not searched): {', '.join(quote_links)}")


@app.command()
def links(
    kind: Annotated[
        str | None, typer.Option("--kind", help="Only sites offering this kind, e.g. lut.")
    ] = None,
) -> None:
    """List free sites without a search API (link-out only; clipforage never scrapes them)."""
    asset_kind: Kind | None = None
    if kind is not None:
        try:
            asset_kind = Kind(kind)
        except ValueError:
            valid = ", ".join(k.value for k in Kind)
            typer.echo(f"error: invalid kind {kind!r}; choose one of: {valid}", err=True)
            raise typer.Exit(code=1) from None
    found = linkouts_for(asset_kind)
    if not found:
        typer.echo("no link-outs for this kind")
    names = [
        f"{link.name} (account required)" if link.requires_account else link.name for link in found
    ]
    width = max([22, *(len(name) for name in names)])
    for link, name in zip(found, names, strict=True):
        kinds = ",".join(k.value for k in link.kinds)
        typer.echo(f"{name:<{width}} {kinds:<34} {link.license_note}")
        typer.echo(f"     {link.url}")


@app.command()
def describe(
    text: Annotated[str, typer.Argument(help="What you remember, e.g. a cat crying at dinner.")],
    kind: Annotated[
        str | None, typer.Option("--kind", help="Asset kind you want, e.g. video or audio_sfx.")
    ] = None,
    style: Annotated[
        str | None, typer.Option("--style", help="Editing style: any, meme, green_screen, ...")
    ] = None,
    backend: Annotated[
        str | None,
        typer.Option("--backend", help="auto, claude_cli, anthropic or openai (default: config)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Turn a half-remembered description into search queries with an optional LLM backend."""
    asset_kind: Kind | None = None
    if kind is not None:
        try:
            asset_kind = Kind(kind)
        except ValueError:
            valid = ", ".join(k.value for k in Kind)
            typer.echo(f"error: invalid kind {kind!r}; choose one of: {valid}", err=True)
            raise typer.Exit(code=1) from None
    if style is not None and style not in STYLES:
        valid = ", ".join(STYLES)
        typer.echo(f"error: invalid style {style!r}; choose one of: {valid}", err=True)
        raise typer.Exit(code=1)
    try:
        result = assist.describe(text, kind=asset_kind, style=style, backend=backend)
    except assist.AssistDisabledError as exc:
        typer.echo(f"error: {exc}", err=True)
        typer.echo(
            f"hint: add `enabled = true` under [assist] in {config.config_path()}, "
            "or set CLIPFORAGE_ASSIST_ENABLED=1 (assist is optional; search works without it)",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except assist.AssistError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
        return
    model = f" ({result.model})" if result.model else ""
    typer.echo(f"backend: {result.backend}{model}")
    if result.names:
        typer.echo(f"names:   {', '.join(result.names)}")
    if result.kind_hint is not None:
        typer.echo(f"kind:    {result.kind_hint.value}")
    typer.echo("queries:")
    for i, query in enumerate(result.queries, start=1):
        typer.echo(f"  {i}. {query}")
    if result.notes:
        typer.echo(f"notes:   {result.notes}")
    if result.rights_note:
        typer.echo(f"rights:  {result.rights_note}")
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "not reported"
    typer.echo(f"cost:    {cost}")


@app.command("resolve-import")
def resolve_import(
    library: Annotated[
        Path | None,
        typer.Option(
            "--library",
            help="Folder to import (default: \\[library] dir in config.toml, else "
            "~/Downloads/clipforage).",
        ),
    ] = None,
    bin_name: Annotated[
        str, typer.Option("--bin", help="Media pool bin to import into.")
    ] = "clipforage",
    only_new: Annotated[
        bool,
        typer.Option("--only-new/--all", help="Skip files already in the bin (default)."),
    ] = True,
    recursive: Annotated[
        bool, typer.Option("--recursive", help="Include files in subfolders.")
    ] = False,
) -> None:
    """Import the library folder into the current DaVinci Resolve project's media pool."""
    library = library or config.library_dir()
    if not library.is_dir():
        typer.echo(f"error: library folder not found: {library}", err=True)
        raise typer.Exit(code=1)
    try:
        resolve = resolve_editor.connect()
        report = resolve_editor.import_library(
            resolve, library, bin_name=bin_name, only_new=only_new, recursive=recursive
        )
    except (resolve_editor.ResolveNotAvailable, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"library  {library}")
    typer.echo(f"bin      {report.bin_name}")
    for label, paths in (
        ("imported", report.imported),
        ("skipped (already in bin)", report.skipped_existing),
        ("failed", report.failed),
    ):
        typer.echo(f"{label}: {len(paths)}")
        for path in paths:
            typer.echo(f"  {Path(path).name}")


LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the web UI in a browser.")
    ] = True,
    reload: Annotated[
        bool, typer.Option("--reload", help="Auto-reload on code changes (dev).")
    ] = False,
    allow_remote: Annotated[
        bool,
        typer.Option("--allow-remote", help="Allow a host other than 127.0.0.1 / localhost."),
    ] = False,
) -> None:
    """Serve the local web UI (FastAPI) on 127.0.0.1."""
    import webbrowser

    import uvicorn

    if reload and allow_remote:
        typer.echo("error: use --reload without --allow-remote", err=True)
        raise typer.Exit(code=1)
    if host not in LOCAL_HOSTS and not allow_remote:
        typer.echo(
            f"error: refusing to bind {host!r}; the web UI is meant for 127.0.0.1 only "
            "(pass --allow-remote to override)",
            err=True,
        )
        raise typer.Exit(code=1)
    if allow_remote:
        typer.echo(
            f"warning: --allow-remote disables the Host check; binding {host!r} makes the UI "
            "(downloads, API keys, settings) reachable from other machines and it has "
            "NO authentication",
            err=True,
        )
    url = f"http://{host}:{port}/"
    typer.echo(f"clipforage web UI at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    if reload:
        uvicorn.run(
            "clipforage.web.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_level="info",
        )
    else:
        from clipforage.web.app import create_app

        web_app = create_app(allowed_hosts=["*"]) if allow_remote else create_app()
        uvicorn.run(web_app, host=host, port=port, log_level="info")


def legacy_main() -> None:
    """Deprecated ``memefinder`` console script: warn once on stderr, then run ``clipforage``."""
    print("memefinder was renamed to clipforage; this alias will be removed", file=sys.stderr)
    app()


if __name__ == "__main__":
    app()
