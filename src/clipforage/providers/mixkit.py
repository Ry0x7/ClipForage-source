"""Mixkit provider: free stock video and sound effects, scraped from Mixkit's list pages.

No key required. Mixkit has no public API, so this provider reads the server-rendered list page
for the query (``https://mixkit.co/free-stock-video/<slug>/`` or
``https://mixkit.co/free-sound-effects/<slug>/``; Mixkit ignores ``?q=``, the slug is the query).
This is a fragile, throttled capability (see ``clipforage.scrape``): exactly one list page per
search (no pagination, no detail pages), robots.txt checked, at most one request per second per
host, results cached 24h. Mixkit's terms forbid bulk downloading, so never add crawling here.
When the markup changes, parsing yields nothing or raises ``ProviderError`` and the search
continues without Mixkit. Disable with ``[scrape] enabled = false``.

Licenses: sound effects are under the Mixkit Sound Effects Free License (commercial use, no
attribution). Videos are either "Free" or "Mixkit Restricted License"; the list page states the
tier only for the items in its JSON-LD (the first few). Items whose tier is not stated get
``commercial_ok=None`` and a "check source" license label.
"""

import html
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from clipforage import scrape
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers.base import ProviderError
from clipforage.scrape import slugify

logger = logging.getLogger("clipforage")

BASE_URL = "https://mixkit.co"
VIDEO_LIST_URL = BASE_URL + "/free-stock-video/{slug}/"
SFX_LIST_URL = BASE_URL + "/free-sound-effects/{slug}/"
VIDEO_FILE_URL = "https://assets.mixkit.co/videos/{id}/{id}-{res}.mp4"
VIDEO_MODAL_URL = BASE_URL + "/free-stock-video/download/{id}/?context=item+grid&type=1080p"
LICENSE_FREE = "Mixkit License"
LICENSE_RESTRICTED = "Mixkit Restricted License"
LICENSE_UNVERIFIED = "Mixkit License (Free or Restricted, check source)"
ATTRIBUTION = "Mixkit (no attribution required)"

_VIDEO_ITEM_RE = re.compile(r'data-item-grid--video-player-item-id-value="(\d+)"')
_VIDEO_TYPE_RE = re.compile(r'data-item-grid--video-player-item-type-value="([^"]*)"')
_THUMB_RE = re.compile(r'<img\s+src="([^"]+)"\s+class="item-grid-video-player__thumb"')
_ALT_RE = re.compile(r'class="item-grid-video-player__thumb"\s+alt="([^"]*)"')
_PREVIEW_RE = re.compile(r'<video\s+src="([^"]+)"')
_LINK_RE = re.compile(
    r'<a\s+href="([^"]+)"\s+class="item-grid-video-player__overlay-link"\s*>(.*?)</a>', re.DOTALL
)
_DESC_RE = re.compile(
    r'class="item-grid-video-player__overlay-video-description"\s*>(.*?)<', re.DOTALL
)
_LD_JSON_RE = re.compile(r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
_LD_ID_RE = re.compile(r"-(\d+)/#video$")

_SFX_ITEM_RE = re.compile(r'data-audio-player-item-id-value="(\d+)"')
_SFX_PREVIEW_RE = re.compile(r'data-audio-player-preview-url-value="([^"]+)"')
_SFX_TYPE_RE = re.compile(r'data-audio-player-item-type-value="([^"]*)"')
_SFX_TITLE_RE = re.compile(r'<h2 class="item-grid-card__title">(.*?)</h2>', re.DOTALL)
_SFX_DURATION_RE = re.compile(r'data-test-id="duration"\s*>\s*([0-9:]+)\s*<')
_SFX_MODAL_RE = re.compile(r'data-download--button-modal-url-value="([^"]+)"')
_MODAL_FILE_RE = re.compile(r'data-download--modal-url-value="([^"]+)"')
_ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


def parse_clock(text: str) -> float | None:
    """``"0:04"`` / ``"1:02:03"`` -> seconds; None when it is not a clock time."""
    parts = text.strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def parse_iso_duration(text: str) -> float | None:
    """``"PT0M15S"`` -> 15.0; None when unparseable."""
    match = _ISO_DURATION_RE.match(text.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _blocks(page: str, item_re: re.Pattern[str]) -> list[tuple[str, str]]:
    """Split ``page`` into ``(item_id, markup)`` from each item marker to the next."""
    matches = list(item_re.finditer(page))
    # Each block starts at the tag carrying the marker, so earlier attributes are included.
    starts = [max(page.rfind("<div", 0, m.start()), 0) for m in matches]
    ends = [*starts[1:], len(page)] if starts else []
    return [(m.group(1), page[s:e]) for m, s, e in zip(matches, starts, ends, strict=True)]


def _video_objects(page: str) -> dict[str, dict[str, Any]]:
    """JSON-LD ``VideoObject`` entries on a list page, keyed by Mixkit video id."""
    found: dict[str, dict[str, Any]] = {}
    for raw in _LD_JSON_RE.findall(page):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "VideoObject":
                continue
            match = _LD_ID_RE.search(str(node.get("@id", "")))
            if match:
                found[match.group(1)] = node
    return found


def _video_license(node: dict[str, Any] | None) -> tuple[str, bool | None, str | None]:
    """``(license label, commercial_ok, tier)`` from a JSON-LD node (None = tier not stated)."""
    if node is None:
        return LICENSE_UNVERIFIED, None, None
    notice = str(node.get("copyrightNotice") or "").lower()
    if "restricted" in notice:
        return LICENSE_RESTRICTED, None, "restricted"
    if notice == "free":
        return LICENSE_FREE, True, "free"
    return LICENSE_UNVERIFIED, None, None


class MixkitProvider:
    """Scrapes one Mixkit list page per search (video and sound effects)."""

    id = "mixkit"
    kinds = frozenset({Kind.VIDEO, Kind.AUDIO_SFX})
    requires_key = False
    scrapes = True
    SEARCH_TTL_S = 86400
    cache_empty_results = True

    def __init__(
        self, client: httpx.Client | None = None, throttle: scrape.HostThrottle | None = None
    ) -> None:
        self._client = client
        self._throttle = throttle

    def _fetch(self, url: str) -> str:
        return scrape.fetch_html(
            url, provider=self.id, client=self._client, throttle=self._throttle
        )

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return assets for ``kind`` from the query's list page; raise ProviderError on failure."""
        if kind not in self.kinds:
            return []
        try:
            if not scrape.enabled():
                raise ProviderError(self.id, scrape.DISABLED_MESSAGE)
            slug = slugify(query)
            if not slug:
                return []
            if kind == Kind.VIDEO:
                url = VIDEO_LIST_URL.format(slug=slug)
                try:
                    page = self._fetch(url)
                except scrape.ScrapeHTTPError as e:
                    if e.status == 404:  # no list page for this slug: no results
                        return []
                    raise
                return self._parse_videos(page, url)
            url = SFX_LIST_URL.format(slug=slug)
            try:
                page = self._fetch(url)
            except scrape.ScrapeHTTPError as e:
                if e.status == 404:
                    return []
                raise
            return self._parse_sfx(page, url)
        except ProviderError as e:
            raise ProviderError(self.id, e.message) from None
        except Exception as e:  # noqa: BLE001 - never let any other exception escape search
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from None

    def _parse_videos(self, page: str, list_url: str) -> list[Asset]:
        ld = _video_objects(page)
        assets: list[Asset] = []
        for item_id, block in _blocks(page, _VIDEO_ITEM_RE):
            item_type = _VIDEO_TYPE_RE.search(block)
            if item_type and item_type.group(1).lower() != "video":
                continue
            link = _LINK_RE.search(block)
            alt = _ALT_RE.search(block)
            title = _text(link.group(2)) if link else ""
            if not title and alt:
                title = _text(alt.group(1)).rstrip(".")
            title = title or f"Mixkit video {item_id}"
            thumb = _THUMB_RE.search(block)
            preview = _PREVIEW_RE.search(block)
            desc = _DESC_RE.search(block)
            node = ld.get(item_id)
            label, commercial_ok, tier = _video_license(node)
            duration = None
            if node and node.get("duration"):
                duration = parse_iso_duration(str(node["duration"]))
            content_url = str(node.get("contentUrl") or "") if node else ""
            assets.append(
                Asset(
                    provider=self.id,
                    id=f"video-{item_id}",
                    kind=Kind.VIDEO,
                    title=title,
                    source_url=urljoin(BASE_URL, html.unescape(link.group(1)))
                    if link
                    else list_url,
                    preview_url=html.unescape(thumb.group(1)) if thumb else None,
                    media_url=html.unescape(preview.group(1)) if preview else None,
                    media_type="video/mp4",
                    license=label,
                    attribution_text=ATTRIBUTION,
                    commercial_ok=commercial_ok,
                    duration_s=duration,
                    extra={
                        "mixkit_id": item_id,
                        "license_tier": tier,
                        "license_url": "https://mixkit.co/license/",
                        "download_url": content_url or None,
                        "description": _text(desc.group(1)) if desc else None,
                    },
                )
            )
        return assets

    def _parse_sfx(self, page: str, list_url: str) -> list[Asset]:
        assets: list[Asset] = []
        for item_id, block in _blocks(page, _SFX_ITEM_RE):
            item_type = _SFX_TYPE_RE.search(block)
            if item_type and item_type.group(1).lower() != "sfx":
                continue
            preview = _SFX_PREVIEW_RE.search(block)
            if not preview:
                continue
            title_match = _SFX_TITLE_RE.search(block)
            duration = _SFX_DURATION_RE.search(block)
            modal = _SFX_MODAL_RE.search(block)
            title = (_text(title_match.group(1)) if title_match else "") or (
                f"Mixkit sound {item_id}"
            )
            assets.append(
                Asset(
                    provider=self.id,
                    id=f"sfx-{item_id}",
                    kind=Kind.AUDIO_SFX,
                    title=title,
                    source_url=list_url,
                    preview_url=None,
                    media_url=html.unescape(preview.group(1)),
                    media_type="audio/mpeg",
                    license=LICENSE_FREE,
                    attribution_text=ATTRIBUTION,
                    commercial_ok=True,
                    duration_s=parse_clock(duration.group(1)) if duration else None,
                    extra={
                        "mixkit_id": item_id,
                        "license_tier": "free",
                        "license_url": "https://mixkit.co/license/#sfxFree",
                        "modal_url": urljoin(BASE_URL, html.unescape(modal.group(1)))
                        if modal
                        else None,
                    },
                )
            )
        return assets

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan an HTTP download of the full file (at most two extra, throttled requests).

        Video: the JSON-LD ``contentUrl`` when the list page gave one; else the file URL from
        the item's 1080p download modal (``VIDEO_MODAL_URL``, which Mixkit answers with the
        best file it has, e.g. 720p for Restricted items); else ``<id>-1080.mp4`` after a HEAD
        (``<id>-720.mp4`` when that is 403/404). Sound effect: the file URL from the item's
        download modal; if that cannot be parsed, the preview mp3 with
        ``extra["download_fallback"]`` set on the plan's asset.
        """
        mixkit_id = str(asset.extra.get("mixkit_id") or asset.id.rsplit("-", 1)[-1])
        if not mixkit_id.isdigit():
            raise ProviderError(self.id, f"asset {asset.id} has no Mixkit id")
        if asset.kind == Kind.VIDEO:
            url, ext = self._video_file(asset, mixkit_id)
            return DownloadPlan(asset=asset, method="http", url=url, ext=ext)
        if asset.kind == Kind.AUDIO_SFX:
            return self._sfx_plan(asset)
        raise ProviderError(self.id, f"cannot download kind {asset.kind}")

    def _modal_file(self, modal_url: str, default_ext: str) -> tuple[str | None, str]:
        """``(file_url, ext)`` from a download modal, or ``(None, reason)``."""
        try:
            modal = self._fetch(modal_url)
        except ProviderError as e:
            return None, f"download modal failed: {e.message}"
        match = _MODAL_FILE_RE.search(modal)
        if not match or not html.unescape(match.group(1)).startswith("https://"):
            return None, "download modal had no file URL"
        file_url = html.unescape(match.group(1))
        tail = file_url.split("?", 1)[0].rsplit("/", 1)[-1]
        ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else default_ext
        if not ext.isalnum() or len(ext) > 4:
            ext = default_ext
        return file_url, ext

    def _video_file(self, asset: Asset, mixkit_id: str) -> tuple[str, str]:
        known = asset.extra.get("download_url")
        if isinstance(known, str) and known.startswith("https://"):
            return known, "mp4"
        file_url, ext_or_reason = self._modal_file(VIDEO_MODAL_URL.format(id=mixkit_id), "mp4")
        if file_url:
            return file_url, ext_or_reason
        logger.warning("mixkit: %s for %s; guessing the file URL", ext_or_reason, asset.id)
        url_1080 = VIDEO_FILE_URL.format(id=mixkit_id, res=1080)
        try:
            response = scrape.request(
                "HEAD", url_1080, provider=self.id, client=self._client, throttle=self._throttle
            )
        except ProviderError as e:
            logger.warning("mixkit: HEAD %s failed (%s); trying 1080 anyway", url_1080, e.message)
            return url_1080, "mp4"
        if response.status_code in (403, 404):  # the asset bucket answers 403 for a missing file
            return VIDEO_FILE_URL.format(id=mixkit_id, res=720), "mp4"
        return url_1080, "mp4"

    def _sfx_plan(self, asset: Asset) -> DownloadPlan:
        modal_url = asset.extra.get("modal_url")
        reason = "no download modal URL in the list page"
        if isinstance(modal_url, str) and modal_url:
            file_url, ext_or_reason = self._modal_file(modal_url, "wav")
            if file_url:
                return DownloadPlan(asset=asset, method="http", url=file_url, ext=ext_or_reason)
            reason = ext_or_reason
        if not asset.media_url:
            raise ProviderError(self.id, f"asset {asset.id}: {reason} and no preview")
        logger.warning("mixkit: %s for %s; using the preview mp3", reason, asset.id)
        flagged = asset.model_copy(
            update={"extra": {**asset.extra, "download_fallback": f"preview mp3 ({reason})"}}
        )
        return DownloadPlan(asset=flagged, method="http", url=asset.media_url, ext="mp3")
