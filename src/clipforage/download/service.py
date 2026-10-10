"""Download any yt-dlp-supported URL as mp4, mp3, or a clip, and write a license sidecar."""

import hashlib
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yt_dlp
import yt_dlp.cookies
import yt_dlp.utils

from clipforage import config
from clipforage.config import default_library_dir
from clipforage.download import binaries
from clipforage.download.sidecar import write_sidecar
from clipforage.http import USER_AGENT, get_client
from clipforage.models import QUALITIES, Asset, DownloadPlan, DownloadResult, Kind

ProgressHook = Callable[[dict[str, Any]], None]

FINAL_FRACTION_KEY = "fraction"
"""Progress key that marks clipforage's single terminal event.

Contract: only clipforage's own final ``finished`` event (built by ``finished_event``) carries
``fraction == 1.0``; yt-dlp's per-file ``finished`` events pass through without it. Consumers
detect the one terminal event with ``status == "finished" and FINAL_FRACTION_KEY in event``.
"""


def finished_event(filename: Path | str, **extra: Any) -> dict[str, Any]:
    """Build clipforage's final progress event: ``status="finished"``, ``fraction`` 1.0.

    ``filename`` is stored as ``str``; ``extra`` keys (e.g. ``downloaded_bytes``) are merged in.
    """
    return {"status": "finished", "filename": str(filename), FINAL_FRACTION_KEY: 1.0, **extra}


YdlFactory = Callable[[dict[str, Any]], Any]

MP4_FORMAT = "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
MP3_FORMAT = "bestaudio/best"
OUTTMPL = "%(title).120B [%(id)s].%(ext)s"

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_STEM_LEN = 120
PROGRESS_EVERY_BYTES = 256 * 1024
PROGRESS_EVERY_S = 0.25
VIDEO_EXTS = frozenset({"mp4", "webm", "mov", "mkv", "m4v"})
AUDIO_EXTS = frozenset({"mp3", "ogg", "wav", "flac", "m4a"})
CLIP_VIDEO_ARGS = (
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-f", "mp4",
)  # fmt: skip
CLIP_AUDIO_ARGS = ("-vn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3")
# VP9 with an alpha channel in WebM: what Premiere, Resolve and CapCut import as transparent.
KEY_VIDEO_ARGS = (
    "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
    "-crf", "30", "-b:v", "0", "-row-mt", "1", "-c:a", "libopus", "-f", "webm",
)  # fmt: skip
KEY_COLOR = "0x00FF00"
KEY_BLEND = 0.1
KEY_TOLERANCE_MAX = 120
_DURATION_RE = re.compile(r"^\s*Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_STDERR_TAIL_LINES = 20
CLIP_MAX_WALL_S = 300.0
# Input options for http(s) URLs: 30 s read timeout (microseconds) and reconnects.
CLIP_HTTP_INPUT_ARGS = (
    "-rw_timeout", "30000000",
    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
)  # fmt: skip
_TIME_RE = re.compile(r"^\d+(?:\.\d+)?$|^\d+:\d{1,2}(?:\.\d+)?$|^\d+:\d{1,2}:\d{1,2}(?:\.\d+)?$")
# Same grammar as yt-dlp's CLI: BROWSER[+KEYRING][:PROFILE][::CONTAINER]
_COOKIES_RE = re.compile(
    r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
    """
)


class DownloadError(Exception):
    """A download failed; the message is user-facing and includes a fix hint."""


class _SilentLogger:
    """yt-dlp logger that prints nothing; ``error`` messages are kept for the final error."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.errors.append(msg)


def _parse_time(text: str) -> float:
    text = text.strip()
    if not _TIME_RE.match(text):
        raise ValueError(f"invalid time {text!r}; use SS, MM:SS or HH:MM:SS")
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def parse_clip(spec: str) -> tuple[float, float]:
    """Parse ``START-END`` (``SS``, ``MM:SS`` or ``HH:MM:SS``, decimals allowed) into seconds."""
    parts = spec.strip().split("-")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(f"invalid clip {spec!r}; expected START-END, e.g. 00:05-00:12 or 10-25")
    start, end = _parse_time(parts[0]), _parse_time(parts[1])
    if end <= start:
        raise ValueError(f"invalid clip {spec!r}; end must be after start")
    return start, end


def parse_cookies_from_browser(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` like yt-dlp's CLI.

    Returns ``(browser, profile, keyring, container)``, the order yt-dlp's
    ``cookiesfrombrowser`` option expects. Raises ``ValueError`` on bad input.
    """
    supported = ", ".join(sorted(yt_dlp.cookies.SUPPORTED_BROWSERS))
    mobj = _COOKIES_RE.fullmatch(spec)
    if mobj is None:
        raise ValueError(
            f"invalid cookies-from-browser value {spec!r}; expected "
            f"BROWSER[+KEYRING][:PROFILE][::CONTAINER]. Supported browsers are: {supported}"
        )
    browser, keyring, profile, container = mobj.group("name", "keyring", "profile", "container")
    browser = browser.lower()
    if browser not in yt_dlp.cookies.SUPPORTED_BROWSERS:
        raise ValueError(
            f'unsupported browser specified for cookies: "{browser}". '
            f"Supported browsers are: {supported}"
        )
    if keyring is not None:
        keyring = keyring.upper()
        if keyring not in yt_dlp.cookies.SUPPORTED_KEYRINGS:
            raise ValueError(
                f'unsupported keyring specified for cookies: "{keyring}". '
                f"Supported keyrings are: {', '.join(sorted(yt_dlp.cookies.SUPPORTED_KEYRINGS))}"
            )
    return browser, profile, keyring, container


def _is_youtube(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


def asset_from_url(url: str, info: dict[str, Any] | None = None, kind: Kind = Kind.VIDEO) -> Asset:
    """Build an ``Asset`` for an arbitrary URL from yt-dlp's info dict (license unknown)."""
    info = info or {}
    host = urlparse(url).hostname or ""
    extractor = info.get("extractor_key")
    if _is_youtube(host):
        provider = "youtube"
    elif extractor:
        provider = str(extractor).lower()
    else:
        provider = "url"
    asset_id = str(info.get("id") or hashlib.sha1(url.encode("utf-8")).hexdigest()[:11])
    title = str(info.get("title") or url)
    source_url = str(info.get("webpage_url") or url)
    uploader = info.get("uploader") or "unknown"
    via = extractor or host or "unknown"
    attribution = (
        f'"{title}" by {uploader} via {via} ({source_url}). '
        "Third-party content; license unknown, check before use."
    )
    duration = info.get("duration")
    return Asset(
        provider=provider,
        id=asset_id,
        kind=kind,
        title=title,
        source_url=source_url,
        license="unknown",
        attribution_text=attribution,
        commercial_ok=None,
        duration_s=float(duration) if duration is not None else None,
    )


def clip_label(start: float, end: float) -> str:
    """Return ``"<start>-<end>"`` with up to 3 decimals, no trailing zeros (``2-6.5``)."""

    def _fmt(value: float) -> str:
        return f"{value + 0.0:.3f}".rstrip("0").rstrip(".")

    return f"{_fmt(start)}-{_fmt(end)}"


def _free_path(path: Path, suffix: str | None = None) -> Path:
    """Return ``path``, or ``<stem> (2)<suffix>``, ``(3)``, ... if it already exists.

    ``suffix`` is a compound extension kept whole (``".key.webm"`` gives ``x (2).key.webm``,
    not ``x.key (2).webm``); ``path``'s name must end with it. None uses ``path.suffix``.
    """
    if suffix is None:
        base, suffix = path.stem, path.suffix
    else:
        base = path.name[: -len(suffix)]
    candidate = path
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{base} ({n}){suffix}")
        n += 1
    return candidate


def video_format_for(quality: str | None) -> str:
    """yt-dlp format selector for ``quality``: ``MP4_FORMAT``, capped at a max height.

    ``"best"`` / None return ``MP4_FORMAT`` unchanged; a height such as ``"720"`` adds
    ``[height<=720]`` to every ``bv*`` / ``b`` clause and keeps a bare ``b`` as last resort.
    Raises ``DownloadError`` for a value not in ``QUALITIES``.
    """
    check_quality(quality)
    if quality is None or quality == "best":
        return MP4_FORMAT
    cap = f"[height<={quality}]"
    return (
        f"bv*[ext=mp4][vcodec^=avc1]{cap}+ba[ext=m4a]/bv*[ext=mp4]{cap}+ba[ext=m4a]"
        f"/b[ext=mp4]{cap}/b{cap}/b"
    )


def check_quality(quality: str | None) -> None:
    """Raise ``DownloadError`` unless ``quality`` is None or one of ``QUALITIES``."""
    if quality is not None and quality not in QUALITIES:
        allowed = ", ".join(QUALITIES)
        raise DownloadError(f"unknown quality {quality!r}\nhint: use one of {allowed}")


def build_ydl_opts(
    out_dir: Path,
    *,
    audio_only: bool,
    clip: tuple[float, float] | None,
    ffmpeg: str | None,
    progress: ProgressHook | None,
    deno: str | None = None,
    cookies_from_browser: tuple[str | None, ...] | None = None,
    quality: str | None = None,
) -> dict[str, Any]:
    """Build yt-dlp options for an mp4 / mp3 / clip download into ``out_dir``.

    ``quality`` caps the mp4 video height (see ``video_format_for``); mp3 ignores it.
    """
    check_quality(quality)
    outtmpl = f"%(title).120B [%(id)s] {clip_label(*clip)}.%(ext)s" if clip else OUTTMPL
    opts: dict[str, Any] = {
        "paths": {"home": str(out_dir)},
        "outtmpl": {"default": outtmpl},
        "windowsfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _SilentLogger(),
    }
    if audio_only:
        opts["format"] = MP3_FORMAT
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        opts["format"] = video_format_for(quality)
        opts["merge_output_format"] = "mp4"
    if clip:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [clip])
        opts["force_keyframes_at_cuts"] = False
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = cookies_from_browser
    if progress:
        opts["progress_hooks"] = [progress]
        opts["postprocessor_hooks"] = [_postprocessor_hook(progress)]
    return opts


def _postprocessor_hook(progress: ProgressHook) -> ProgressHook:
    """Map yt-dlp postprocessor ``started`` events (merge, mp3 extract) to ``postprocessing``.

    Reported once per postprocessor name: yt-dlp attaches the hook twice to user-supplied
    postprocessors (``FFmpegExtractAudio``), so their ``started`` fires twice.
    """
    started: set[Any] = set()

    def hook(event: dict[str, Any]) -> None:
        if event.get("status") == "started":
            name = event.get("postprocessor")
            if name in started:
                return
            started.add(name)
            progress(
                {
                    "status": "postprocessing",
                    "postprocessor": event.get("postprocessor"),
                    "fraction": None,
                }
            )

    return hook


def _hint(message: str) -> str:
    lowered = message.lower()
    cookies_fix = (
        "pass --cookies-from-browser <browser> (e.g. firefox, chrome, edge) "
        f"or set [download] cookies_from_browser in {config.config_path()}"
    )
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return f"YouTube asked for sign-in; {cookies_fix}"
    if "logged-in" in lowered or "login" in lowered or "cookies" in lowered:
        return f"this site requires a login; {cookies_fix}"
    if "javascript runtime" in lowered or "deno" in lowered:
        return "run `clipforage setup` to install Deno, then `clipforage doctor`"
    if "ffmpeg" in lowered:
        return "run `clipforage setup` to install ffmpeg, then `clipforage doctor`"
    return "update yt-dlp (`uv sync --upgrade-package yt-dlp`) and try again"


def download_url(
    url: str,
    out_dir: Path | None = None,
    *,
    audio_only: bool = False,
    clip: tuple[float, float] | None = None,
    progress: ProgressHook | None = None,
    ydl_factory: YdlFactory | None = None,
    cookies_from_browser: str | None = None,
    quality: str | None = None,
) -> DownloadResult:
    """Download ``url`` (any yt-dlp-supported site) and write a license sidecar beside it.

    ``quality`` is a max video height from ``QUALITIES`` (None / ``"best"``: no cap); it is
    ignored for ``audio_only`` and recorded in the sidecar ``extra`` as ``quality``.

    ``cookies_from_browser`` is a yt-dlp ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` spec;
    when None, ``[download] cookies_from_browser`` / ``CLIPFORAGE_COOKIES_FROM_BROWSER`` (legacy
    ``MEMEFINDER_COOKIES_FROM_BROWSER``) is used.
    """
    if cookies_from_browser is None:
        cookies_from_browser = config.get_setting(
            "download", "cookies_from_browser", env="COOKIES_FROM_BROWSER"
        )
    cookies: tuple[str | None, ...] | None = None
    if cookies_from_browser:
        try:
            cookies = parse_cookies_from_browser(cookies_from_browser)
        except ValueError as exc:
            raise DownloadError(str(exc)) from exc
    out_dir = out_dir or default_library_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = binaries.find_tool("ffmpeg")
    ffmpeg = str(ffmpeg_path) if ffmpeg_path else None
    if (clip or audio_only) and ffmpeg is None:
        raise DownloadError("ffmpeg not found; run `clipforage setup`")
    deno_path = binaries.find_tool("deno")
    # Only override yt-dlp's default (Deno on PATH) when setup installed our own copy.
    deno = str(deno_path) if deno_path and deno_path.parent == binaries.bin_dir() else None
    opts = build_ydl_opts(
        out_dir,
        audio_only=audio_only,
        clip=clip,
        ffmpeg=ffmpeg,
        progress=progress,
        deno=deno,
        cookies_from_browser=cookies,
        quality=quality,
    )
    factory = ydl_factory or yt_dlp.YoutubeDL
    try:
        with factory(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        text = str(exc)
        logger = opts["logger"]
        if not text.strip() and logger.errors:
            text = logger.errors[-1]
        raise DownloadError(f"{text}\nhint: {_hint(text)}") from exc
    downloads = (info or {}).get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads else None
    if not filepath:
        raise DownloadError("yt-dlp finished but reported no output file\nhint: update yt-dlp")
    media_path = Path(filepath)
    if progress:
        progress(finished_event(media_path))
    kind = Kind.AUDIO_MUSIC if audio_only else Kind.VIDEO
    asset = asset_from_url(url, info, kind=kind)
    fetched_at = datetime.now(UTC)
    extra: dict[str, Any] = {"format": "mp3" if audio_only else "mp4", "quality": quality or "best"}
    if clip:
        extra["clip"] = {"start_s": clip[0], "end_s": clip[1]}
    sidecar = write_sidecar(media_path, asset, fetched_at, extra)
    return DownloadResult(path=media_path, sidecar=sidecar, fetched_at=fetched_at)


def safe_filename(stem: str) -> str:
    """Make ``stem`` safe as a Windows/POSIX file name (no extension handling)."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", stem).strip().rstrip(". ")
    return cleaned[:_MAX_STEM_LEN].rstrip(". ") or "download"


def download_http(
    plan: DownloadPlan,
    out_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    filename: str | None = None,
    progress: ProgressHook | None = None,
) -> DownloadResult:
    """Stream an ``http`` plan to ``out_dir/<safe filename>.<plan.ext>`` and write a sidecar.

    ``filename`` is the stem (default ``<title> [<id>]``). Raises ``httpx.HTTPStatusError``
    on a non-2xx response; nothing is left on disk when the download fails. ``progress`` gets
    yt-dlp-style dicts (``status``, ``downloaded_bytes``, ``total_bytes``) at most every
    256 KiB, then once with ``status="finished"``.
    """
    if plan.method != "http":
        raise ValueError(f"download_http needs an http plan, got {plan.method!r}")
    out_dir = out_dir or default_library_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = plan.ext.lstrip(".")
    stem = safe_filename(filename or f"{plan.asset.title} [{plan.asset.id}]")
    media_path = out_dir / f"{stem}.{ext}"
    part_path = media_path.with_name(media_path.name + ".part")
    http = client or get_client()
    with http.stream("GET", plan.url, headers=plan.headers) as response:
        response.raise_for_status()
        length = response.headers.get("content-length")
        total = int(length) if length and length.isdigit() else None
        downloaded = 0
        reported = 0
        try:
            with part_path.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress and downloaded - reported >= PROGRESS_EVERY_BYTES:
                        reported = downloaded
                        progress(
                            {
                                "status": "downloading",
                                "downloaded_bytes": downloaded,
                                "total_bytes": total,
                            }
                        )
            if progress and total is not None and reported != downloaded:
                reported = downloaded
                progress(
                    {
                        "status": "downloading",
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                    }
                )
            part_path.replace(media_path)
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise
    if progress:
        progress(
            finished_event(
                media_path,
                downloaded_bytes=downloaded,
                total_bytes=total if total is not None else downloaded,
            )
        )
    fetched_at = datetime.now(UTC)
    sidecar = write_sidecar(media_path, plan.asset, fetched_at, {"format": ext})
    return DownloadResult(path=media_path, sidecar=sidecar, fetched_at=fetched_at)


def _fmt_time(seconds: float) -> str:
    return f"{seconds:.3f}"


def _clip_progress_time(line: str) -> float | None:
    """Return seconds from an ffmpeg ``-progress`` ``out_time_us=`` / ``out_time_ms=`` line."""
    key, _, value = line.strip().partition("=")
    # ffmpeg reports both keys in microseconds (out_time_ms is misnamed upstream).
    if key not in ("out_time_us", "out_time_ms") or not value.isdigit():
        return None
    return int(value) / 1_000_000


def download_http_clip(
    plan: DownloadPlan,
    clip: tuple[float, float],
    out_dir: Path | None = None,
    *,
    ffmpeg: Path | None = None,
    progress: ProgressHook | None = None,
    filename: str | None = None,
    max_wall_s: float | None = None,
) -> DownloadResult:
    """Cut ``clip`` (start, end seconds) out of an ``http`` plan with ffmpeg and write a sidecar.

    ffmpeg reads ``plan.url`` directly (https range seeking) and re-encodes: video plans to
    h264/aac ``.mp4`` (any source container), audio plans (``AUDIO_EXTS``) to ``.mp3``. The
    file is ``<title> [<id>] <start>-<end>.<ext>`` (``clip_label``: up to 3 decimals; ``filename``
    overrides the stem before the range; `` (2)``, `` (3)``, ... is appended before the extension
    if the name is taken), written to ``.part`` first. ``progress`` gets ``fraction`` dicts at most every
    0.25 s, then ``status="finished"``. Raises ``DownloadError`` if ffmpeg is missing, fails, or
    runs longer than ``max_wall_s`` (ffmpeg is killed and the ``.part`` removed). ``None`` means
    ``CLIP_MAX_WALL_S``; ``0`` or negative means no wall-clock limit.
    """
    if max_wall_s is None:
        max_wall_s = CLIP_MAX_WALL_S
    if plan.method != "http":
        raise ValueError(f"download_http_clip needs an http plan, got {plan.method!r}")
    start, end = clip
    if end <= start:
        raise ValueError(f"invalid clip {start}-{end}; end must be after start")
    if ffmpeg is None:
        ffmpeg = binaries.find_tool("ffmpeg")
    if ffmpeg is None:
        raise DownloadError("ffmpeg not found; run `clipforage setup`")
    audio = plan.ext.lstrip(".").lower() in AUDIO_EXTS
    ext = "mp3" if audio else "mp4"
    out_dir = out_dir or default_library_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(filename or f"{plan.asset.title} [{plan.asset.id}]")
    media_path = _free_path(out_dir / f"{stem} {clip_label(start, end)}.{ext}")
    part_path = media_path.with_name(media_path.name + ".part")
    argv = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
    argv += ["-user_agent", USER_AGENT]
    if plan.headers:
        argv += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in plan.headers.items())]
    if plan.url.lower().startswith(("http://", "https://")):
        argv += list(CLIP_HTTP_INPUT_ARGS)
    argv += ["-ss", _fmt_time(start), "-to", _fmt_time(end), "-i", plan.url]
    argv += list(CLIP_AUDIO_ARGS if audio else CLIP_VIDEO_ARGS)
    argv.append(str(part_path))
    duration = end - start
    tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise DownloadError(f"could not run ffmpeg: {exc}\nhint: {_hint('ffmpeg')}") from exc
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        proc.kill()

    watchdog: threading.Timer | None = None
    if max_wall_s > 0:
        watchdog = threading.Timer(max_wall_s, _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()
    try:
        assert proc.stdout is not None and proc.stderr is not None
        stderr = proc.stderr
        reader = threading.Thread(
            target=lambda: tail.extend(line.rstrip() for line in stderr), daemon=True
        )
        reader.start()
        best = 0.0
        last_report = float("-inf")
        for line in proc.stdout:
            seconds = _clip_progress_time(line)
            if seconds is None or progress is None:
                continue
            best = max(best, min(1.0, seconds / duration))
            now = time.monotonic()
            if now - last_report >= PROGRESS_EVERY_S:
                last_report = now
                progress(
                    {
                        "status": "downloading",
                        "downloaded_bytes": None,
                        "total_bytes": None,
                        "fraction": best,
                    }
                )
        returncode = proc.wait()
        if watchdog is not None:
            watchdog.cancel()
        reader.join(timeout=5)
        if timed_out.is_set():
            raise DownloadError(
                f"clip took longer than {max_wall_s:g} s; the remote file is probably badly "
                "interleaved for streaming; download the full file with `clipforage get <ref>` "
                "and trim locally"
            )
        if returncode != 0:
            stderr_tail = "\n".join(tail)
            raise DownloadError(
                f"ffmpeg exited with code {returncode} clipping {plan.url}\n{stderr_tail}\n"
                "hint: check the clip range is inside the media, then `clipforage doctor`"
            )
        if not part_path.is_file():
            raise DownloadError(f"ffmpeg finished but wrote no file: {part_path}")
        part_path.replace(media_path)
    except BaseException:
        if watchdog is not None:
            watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        part_path.unlink(missing_ok=True)
        raise
    if progress:
        # fraction keeps fraction-based hooks (web jobs) at 100% instead of resetting them.
        progress(finished_event(media_path))
    fetched_at = datetime.now(UTC)
    extra: dict[str, Any] = {"format": ext, "clip": {"start_s": start, "end_s": end}}
    sidecar = write_sidecar(media_path, plan.asset, fetched_at, extra)
    return DownloadResult(path=media_path, sidecar=sidecar, fetched_at=fetched_at)


def chroma_similarity(tolerance: int) -> float:
    """Map the UI tolerance (0..120, higher keys more) to ffmpeg ``chromakey`` similarity."""
    clamped = max(0, min(KEY_TOLERANCE_MAX, tolerance))
    return round(0.05 + 0.35 * clamped / KEY_TOLERANCE_MAX, 3)


def chroma_filter(tolerance: int) -> str:
    """Return the ``-vf`` chain that keys out pure green and removes green spill."""
    similarity = chroma_similarity(tolerance)
    return (
        f"chromakey=color={KEY_COLOR}:similarity={similarity}:blend={KEY_BLEND},despill=type=green"
    )


def _parse_duration(line: str) -> float | None:
    """Return seconds from ffmpeg's input ``Duration: HH:MM:SS.cc`` stderr line, else None."""
    match = _DURATION_RE.match(line)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def key_out(
    src: Path,
    *,
    ffmpeg: Path | None = None,
    tolerance: int = 80,
    progress: ProgressHook | None = None,
    max_wall_s: float = CLIP_MAX_WALL_S,
) -> Path:
    """Key the green screen out of ``src`` into a transparent VP9/WebM beside it; return its path.

    The output is ``<stem>.key.webm`` in ``src``'s folder (``<stem> (2).key.webm``, ... if
    taken), written to ``.part`` first. ``tolerance`` (0..120) maps to ``chroma_filter``.
    ``progress`` gets ``fraction`` dicts at most every 0.25 s once the input duration is known
    (none otherwise), then ``status="finished"``. Raises ``DownloadError`` if ffmpeg is missing,
    fails, or runs longer than ``max_wall_s`` (0 or negative: no limit); the ``.part`` is removed
    on failure.
    """
    if ffmpeg is None:
        ffmpeg = binaries.find_tool("ffmpeg")
    if ffmpeg is None:
        raise DownloadError("ffmpeg not found; run `clipforage setup`")
    out = _free_path(src.with_name(src.stem + ".key.webm"), suffix=".key.webm")
    part_path = out.with_name(out.name + ".part")
    argv = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
    argv += ["-i", str(src), "-vf", chroma_filter(tolerance)]
    argv += list(KEY_VIDEO_ARGS)
    argv.append(str(part_path))
    tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    duration: list[float] = []
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise DownloadError(f"could not run ffmpeg: {exc}\nhint: {_hint('ffmpeg')}") from exc
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        proc.kill()

    def _read_stderr(stream: Any) -> None:
        for raw in stream:
            line = raw.rstrip()
            tail.append(line)
            if not duration:
                seconds = _parse_duration(line)
                if seconds:
                    duration.append(seconds)

    watchdog: threading.Timer | None = None
    if max_wall_s > 0:
        watchdog = threading.Timer(max_wall_s, _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()
    try:
        assert proc.stdout is not None and proc.stderr is not None
        reader = threading.Thread(target=_read_stderr, args=(proc.stderr,), daemon=True)
        reader.start()
        best = 0.0
        last_report = float("-inf")
        for line in proc.stdout:
            seconds = _clip_progress_time(line)
            if seconds is None or progress is None or not duration:
                continue
            best = max(best, min(1.0, seconds / duration[0]))
            now = time.monotonic()
            if now - last_report >= PROGRESS_EVERY_S:
                last_report = now
                progress(
                    {
                        "status": "downloading",
                        "downloaded_bytes": None,
                        "total_bytes": None,
                        "fraction": best,
                    }
                )
        returncode = proc.wait()
        if watchdog is not None:
            watchdog.cancel()
        reader.join(timeout=5)
        if timed_out.is_set():
            raise DownloadError(
                f"keying took longer than {max_wall_s:g} s; raise --max-wall-s "
                "(or [download] clip_max_wall_s), 0 disables the limit"
            )
        if returncode != 0:
            stderr_tail = "\n".join(tail)
            raise DownloadError(
                f"ffmpeg exited with code {returncode} keying {src}\n{stderr_tail}\n"
                "hint: check the file is a readable video, then `clipforage doctor`"
            )
        if not part_path.is_file():
            raise DownloadError(f"ffmpeg finished but wrote no file: {part_path}")
        part_path.replace(out)
    except BaseException:
        if watchdog is not None:
            watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        part_path.unlink(missing_ok=True)
        raise
    if progress:
        progress(finished_event(out))
    return out


def download_plan(
    plan: DownloadPlan,
    out_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    progress: ProgressHook | None = None,
    ydl_factory: YdlFactory | None = None,
    cookies_from_browser: str | None = None,
    clip: tuple[float, float] | None = None,
    max_wall_s: float | None = None,
    quality: str | None = None,
) -> DownloadResult:
    """Download a provider ``DownloadPlan``: ``http`` via httpx (or ffmpeg for a ``clip``),
    ``ytdlp`` via ``download_url``. ``max_wall_s`` only applies to http clips (see
    ``download_http_clip``; None keeps its default). ``quality`` only applies to ``ytdlp``
    plans; ``http`` plans are a single stock rendition and ignore it."""
    if plan.method == "http":
        if clip:
            if max_wall_s is None:
                return download_http_clip(plan, clip, out_dir, progress=progress)
            return download_http_clip(plan, clip, out_dir, progress=progress, max_wall_s=max_wall_s)
        return download_http(plan, out_dir, client=client, progress=progress)
    return download_url(
        plan.url,
        out_dir,
        audio_only=plan.ext.lstrip(".") == "mp3",
        clip=clip,
        progress=progress,
        ydl_factory=ydl_factory,
        cookies_from_browser=cookies_from_browser,
        quality=quality,
    )
