"""Fetch caption/subtitle tracks for phrase search.

YouTube: the yt-dlp Python API lists the video's English manual or automatic captions
(``requested_subtitles``) and the VTT file is fetched with the shared httpx client.
Internet Archive: the item's Metadata API file list is searched for a ``.vtt`` / ``.srt``
file, which is downloaded from ``https://archive.org/download/<identifier>/<name>``.

Each fetcher returns ``(cues, lang, auto_generated)``, ``None`` when the asset has no
captions, and raises ``ProviderError`` on failure.
"""

import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
import yt_dlp

from clipforage import config, http
from clipforage.download import binaries
from clipforage.download.service import parse_cookies_from_browser
from clipforage.models import Asset, Cue
from clipforage.phrase.subtitles import parse_subtitles
from clipforage.providers.base import ProviderError
from clipforage.providers.internet_archive import DOWNLOAD_URL, METADATA_URL

YdlFactory = Callable[[dict[str, Any]], Any]
CaptionResult = tuple[list[Cue], str | None, bool]

YOUTUBE_SUB_LANGS: tuple[str, ...] = ("en", "en-orig", "en.*")
IA_SUBTITLE_FORMATS: frozenset[str] = frozenset({"SubRip", "Web Video Text Tracks"})
_SUB_EXTS = (".vtt", ".srt")
_ENGLISH_RE = re.compile(r"(?:^|[^a-z])(en|eng|english)(?:[^a-z]|$)", re.IGNORECASE)


def _decode(content: bytes) -> str:
    """Subtitle bytes -> text: UTF-8 (BOM allowed), else cp1252 (common for old SRTs)."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _youtube_opts(cookies_from_browser: str | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": list(YOUTUBE_SUB_LANGS),
        "subtitlesformat": "vtt",
    }
    # Same rule as download.service: only override yt-dlp's default (Deno on PATH) when
    # `clipforage setup` installed our own copy.
    deno_path = binaries.find_tool("deno")
    if deno_path and deno_path.parent == binaries.bin_dir():
        opts["js_runtimes"] = {"deno": {"path": str(deno_path)}}
    if cookies_from_browser is None:
        cookies_from_browser = config.get_setting(
            "download", "cookies_from_browser", env="COOKIES_FROM_BROWSER"
        )
    if cookies_from_browser:
        try:
            opts["cookiesfrombrowser"] = parse_cookies_from_browser(cookies_from_browser)
        except ValueError:
            pass  # a bad setting must not break phrase search; `get` reports it
    return opts


def fetch_youtube_captions(
    asset: Asset,
    *,
    ydl_factory: YdlFactory | None = None,
    client: httpx.Client | None = None,
    cookies_from_browser: str | None = None,
) -> CaptionResult | None:
    """Return the first English caption track of a YouTube asset, or None if it has none.

    ``cookies_from_browser`` is a yt-dlp ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` spec;
    when None, ``[download] cookies_from_browser`` / ``CLIPFORAGE_COOKIES_FROM_BROWSER`` is used.
    """
    factory = ydl_factory or yt_dlp.YoutubeDL
    opts = _youtube_opts(cookies_from_browser)
    try:
        with factory(opts) as ydl:
            info = ydl.extract_info(asset.source_url, download=False) or {}
    except Exception as e:  # noqa: BLE001 - yt-dlp raises many types
        raise ProviderError("youtube", f"{type(e).__name__}: {e}") from None
    requested = info.get("requested_subtitles") or {}
    if not requested:
        return None
    lang, track = next(iter(requested.items()))
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    auto_generated = lang in automatic and lang not in manual
    if track.get("data"):
        text = str(track["data"])
    else:
        url = track.get("url")
        if not url:
            return None
        http_client = client if client is not None else http.get_client()
        try:
            response = http_client.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("youtube", f"{type(e).__name__}: {e}") from None
        if not response.is_success:
            raise ProviderError("youtube", f"caption HTTP {response.status_code}")
        text = _decode(response.content)
    return parse_subtitles(text, track.get("ext")), lang, auto_generated


def _pick_subtitle(files: list[Any]) -> dict[str, Any] | None:
    candidates = [
        f
        for f in files
        if isinstance(f, dict)
        and f.get("name")
        and (
            str(f["name"]).lower().endswith(_SUB_EXTS)
            or str(f.get("format") or "") in IA_SUBTITLE_FORMATS
        )
    ]
    for f in candidates:
        if _ENGLISH_RE.search(PurePosixPath(str(f["name"])).stem):
            return f
    return candidates[0] if candidates else None


def fetch_ia_captions(asset: Asset, *, client: httpx.Client | None = None) -> CaptionResult | None:
    """Return the cues of an Internet Archive item's subtitle file, or None if it has none."""
    identifier = str(asset.extra.get("identifier") or asset.id)
    http_client = client if client is not None else http.get_client()
    try:
        meta = http_client.get(METADATA_URL.format(identifier=quote(identifier, safe="")))
        if not meta.is_success:
            raise ProviderError("internet_archive", f"metadata HTTP {meta.status_code}")
        data = meta.json()
        files = data.get("files") if isinstance(data, dict) else None
        chosen = _pick_subtitle(files if isinstance(files, list) else [])
        if chosen is None:
            return None
        name = str(chosen["name"])
        url = DOWNLOAD_URL.format(identifier=quote(identifier, safe=""), filename=quote(name))
        response = http_client.get(url)
        if not response.is_success:
            raise ProviderError("internet_archive", f"subtitle HTTP {response.status_code}")
    except ProviderError:
        raise
    except Exception as e:  # noqa: BLE001 - wrap httpx / JSON errors
        raise ProviderError("internet_archive", f"{type(e).__name__}: {e}") from None
    is_vtt = name.lower().endswith(".vtt") or chosen.get("format") == "Web Video Text Tracks"
    fmt = "vtt" if is_vtt else "srt"
    lang = "en" if _ENGLISH_RE.search(PurePosixPath(name).stem) else None
    auto_generated = ".asr." in name.lower()  # IA speech-recognition derivative
    return parse_subtitles(_decode(response.content), fmt), lang, auto_generated
