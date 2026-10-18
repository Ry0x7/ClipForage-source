"""FastAPI app for the local web UI. Meant to be served on 127.0.0.1 only.

Every state-changing request (anything but GET/HEAD/OPTIONS) must send ``X-Clipforage: 1``;
browsers cannot add that header to a
cross-site form post, and there is no CORS middleware.
"""

import asyncio
import mimetypes
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, get_args
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from clipforage import assist, config, recall, rights, safesearch, service
from clipforage.cache import Cache
from clipforage.download import binaries
from clipforage.download.service import parse_clip
from clipforage.http import get_client
from clipforage.linkout import linkouts_for
from clipforage.models import QUALITIES, Asset, Kind, SearchFilters, Style
from clipforage.providers import registry
from clipforage.providers.base import Provider, ProviderError
from clipforage.web.jobs import JobManager
from clipforage.web.schemas import (
    AssetOut,
    AssistBackendOut,
    AssistIn,
    AssistKeysOut,
    AssistOut,
    AssistSettingsIn,
    AssistSettingsOut,
    AssistStatusOut,
    DoctorOut,
    DownloadAccepted,
    DownloadRequest,
    HistoryRow,
    JobOut,
    KeyIn,
    KeyOut,
    KeyOutRequest,
    LibraryDirIn,
    LibraryDirOut,
    OpenIn,
    OpenOut,
    PhraseHitOut,
    PhraseResponse,
    ProviderInfo,
    RecallResponse,
    RightsOut,
    SearchErrorOut,
    SearchResponse,
    SearchSettingsIn,
    SearchSettingsOut,
    SettingsOut,
    ToolInfo,
    YtDlpInfo,
)

CSRF_HEADER = "X-Clipforage"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ORIENTATIONS: tuple[str, ...] = ("landscape", "portrait", "square")
STYLES: tuple[str, ...] = get_args(Style)
PREVIEW_CACHE_CONTROL = "public, max-age=3600"
LOCAL_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")
YTDLP_ONLY_PROVIDER = "youtube"
MEDIA_RESOLVE_PROVIDER = "internet_archive"  # no media_url; its resolved http file is playable
PHRASE_LINKOUT = "PlayPhrase"
PHRASE_MAX_VIDEOS = 25
PHRASE_MAX_LIMIT = 100
PHRASE_MAX_PER_VIDEO = 50
CLIP_MAX_WALL_S_CAP = 7200.0
ASSIST_KEY_IDS: tuple[str, ...] = ("anthropic", "openai")
ASSIST_TIMEOUT_GRACE_S = 15.0
"""Extra wall time on top of ``[assist] timeout_s`` before ``POST /api/assist`` gives up."""
MEDIA_FORWARD_HEADERS: tuple[str, ...] = ("Content-Length", "Content-Range", "Accept-Ranges")
UPSTREAM_PASSTHROUGH_ERRORS = frozenset({404, 416})  # not upstream faults; 5xx stays 502
# Common two-label public suffixes for _same_site. A pragmatic list, not the Public Suffix List
# (no new dependency); multi-tenant hosts such as github.io are out of scope.
_MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.nz", "net.nz", "org.nz",
        "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
        "co.kr", "or.kr",
        "com.br", "net.br", "org.br",
        "com.cn", "net.cn", "org.cn",
        "com.mx", "com.ar", "com.tr",
        "co.za", "org.za",
        "co.in", "net.in", "org.in",
        "co.il", "com.sg", "com.hk", "com.tw", "com.my", "com.ph", "com.vn", "com.ua", "com.pl",
        "co.id", "or.id", "co.th",
        "com.eg", "com.ng", "com.pk", "com.sa", "com.ae",
        "com.co", "com.pe", "com.ve", "com.uy", "com.ec", "co.ve",
    }
)  # fmt: skip


def static_dir() -> Path:
    """Return the packaged ``static`` folder (a real directory in the wheel and in src)."""
    return Path(str(files("clipforage.web") / "static"))


def open_in_file_manager(path: Path) -> None:
    """Show ``path`` (a folder) in the OS file manager."""
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)])


def _provider_ids() -> set[str]:
    return {p["id"] for p in service.list_providers()}


def _asset_out(asset: Asset) -> AssetOut:
    """Serialise an asset with its ``<provider>:<id>`` ref and rights signal."""
    return AssetOut(
        **asset.model_dump(),
        ref=f"{asset.provider}:{asset.id}",
        rights=RightsOut(**rights.classify(asset).model_dump()),
    )


def _attach_history(assets: list[AssetOut]) -> list[AssetOut]:
    """Set ``history_id`` / ``history_path`` on assets with a downloaded file still in the
    library folder. Best effort: a cache or filesystem error leaves the assets unchanged."""
    if not assets:
        return assets
    try:
        library = config.library_dir().resolve()
        result = []
        with Cache() as cache:
            for out in assets:
                row = cache.latest_download_for_asset(out.provider, out.id)
                if row is not None:
                    target = Path(row["path"]).resolve()
                    if target.is_relative_to(library) and target.exists():
                        update = {"history_id": row["id"], "history_path": str(target)}
                        out = out.model_copy(update=update)
                result.append(out)
    except (sqlite3.Error, OSError):
        return assets
    return result


def _masked_key(key_id: str) -> str | None:
    key = config.get_key(key_id)
    return config.mask_key(key) if key else None


def _assist_settings() -> AssistSettingsOut:
    return AssistSettingsOut(
        enabled=assist.configured_enabled(),
        backend=assist.configured_backend(),
        model=config.get_setting("assist", "model", env="ASSIST_MODEL"),
        **{
            f"{backend}_model": config.get_setting(
                "assist", f"{backend}_model", env=f"ASSIST_{backend.upper()}_MODEL"
            )
            for backend in assist.BACKENDS
        },
        keys=AssistKeysOut(**{key_id: _masked_key(key_id) for key_id in ASSIST_KEY_IDS}),
    )


def _settings_out() -> SettingsOut:
    provider_list = service.list_providers()
    keys: dict[str, str | None] = {}
    for p in provider_list:
        if p["requires_key"]:
            keys[p["id"]] = _masked_key(p["id"])
    return SettingsOut(
        library_dir=str(config.library_dir()),
        config_path=str(config.config_path()),
        keys=keys,
        providers=[ProviderInfo(**p) for p in provider_list],
        qualities=list(QUALITIES),
        assist=_assist_settings(),
        search=SearchSettingsOut(safe=safesearch.configured_safe()),
    )


def _cached_asset(provider: str, asset_id: str) -> tuple[Provider, Asset]:
    """Return the provider and cached asset for ``provider:asset_id`` or raise 404/503."""
    try:
        return service.resolve_ref(f"{provider}:{asset_id}")
    except service.RefError as exc:
        raise HTTPException(404, str(exc)) from None
    except service.CacheUnavailableError as exc:
        raise HTTPException(503, str(exc)) from None


def _content_disposition(name: str) -> str:
    """``inline`` disposition with an ASCII ``filename`` plus RFC 5987 ``filename*`` if needed."""
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    header = f'inline; filename="{fallback}"'
    if fallback != name:
        header += f"; filename*=UTF-8''{quote(name, safe='')}"
    return header


def _library_file(history_id: int, column: str) -> FileResponse:
    """Serve the ``path`` or ``sidecar`` of a history row; only files inside the library."""
    target = _library_path(history_id, column)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition(target.name)},
    )


def _library_path(history_id: int, column: str) -> Path:
    """Resolve the ``path`` or ``sidecar`` of a history row: 404 unknown row or missing file,
    403 outside the library folder, 503 cache unavailable."""
    try:
        with Cache() as cache:
            row = cache.get_download(history_id)
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, f"cache unavailable: {exc}") from None
    if row is None:
        raise HTTPException(404, f"unknown download {history_id}")
    library = config.library_dir().resolve()
    target = Path(row[column]).resolve()
    if not target.is_relative_to(library):
        raise HTTPException(403, "path is outside the library folder")
    if not target.is_file():
        raise HTTPException(404, "file no longer exists")
    return target


def _same_site(host_a: str, host_b: str) -> bool:
    """True if the hosts are equal or share a registrable domain (``archive.org`` and
    ``dn721805.ca.archive.org``; ``bbc.co.uk`` and ``www.bbc.co.uk``). The registrable domain is
    the last two DNS labels, or the last three when the last two are in ``_MULTI_LABEL_SUFFIXES``
    (so ``a.co.uk`` and ``b.co.uk`` differ). Redirects within one site are allowed; off-site ones
    are not.
    """
    a = host_a.lower().rstrip(".")
    b = host_b.lower().rstrip(".")
    if a == b:
        return True
    labels_a, labels_b = a.split("."), b.split(".")
    if len(labels_a) < 2 or len(labels_b) < 2:
        return False
    size = 3 if ".".join(labels_a[-2:]) in _MULTI_LABEL_SUFFIXES else 2
    if len(labels_a) < size or len(labels_b) < size:
        return False
    return labels_a[-size:] == labels_b[-size:]


def _proxy_stream(
    url: str | None,
    what: str,
    request_headers: dict[str, str] | None = None,
    forward: tuple[str, ...] = (),
) -> Response:
    """Stream ``url`` back to the browser; http(s) only, redirects only within the same site.

    ``request_headers`` are sent upstream (e.g. ``Range``); ``forward`` names upstream response
    headers copied onto ours. The upstream status (200/206) is passed through, as are 404 and 416
    (with an empty body); other errors become 502.
    """
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(404, f"asset has no {what}")
    client = get_client()
    try:
        upstream = client.send(
            client.build_request("GET", url, headers=request_headers), stream=True
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"{what} fetch failed: {exc}") from None
    if upstream.url.scheme not in ("http", "https") or not _same_site(
        upstream.url.host, httpx.URL(url).host
    ):
        upstream.close()
        raise HTTPException(502, f"{what} fetch failed: redirected to another host")
    if upstream.status_code in UPSTREAM_PASSTHROUGH_ERRORS:
        upstream.close()
        return Response(status_code=upstream.status_code)
    if upstream.status_code >= 400:
        upstream.close()
        raise HTTPException(502, f"{what} fetch failed: HTTP {upstream.status_code}")
    headers = {"Cache-Control": PREVIEW_CACHE_CONTROL}
    for name in forward:
        if name in upstream.headers:
            headers[name] = upstream.headers[name]
    # Raw bytes so Content-Length/Content-Range stay valid; Content-Encoding is forwarded too.
    if "content-encoding" in upstream.headers:
        headers["Content-Encoding"] = upstream.headers["content-encoding"]

    def body() -> Iterator[bytes]:
        try:
            yield from upstream.iter_raw()
        finally:
            upstream.close()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


def create_app(allowed_hosts: list[str] | None = None) -> FastAPI:
    """Build the FastAPI app (routes, static files, header guard, host check, job manager).

    ``allowed_hosts`` are extra ``Host`` header values accepted on top of the loopback names
    (used by ``serve --allow-remote``); anything else gets a 400 (DNS-rebinding guard).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry.load_builtins()  # once, before worker threads race to do it
        yield
        app.state.jobs.shutdown(wait=False)

    app = FastAPI(title="ClipForage", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.jobs = JobManager()
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[*LOCAL_ALLOWED_HOSTS, *(allowed_hosts or [])]
    )

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": f"missing {CSRF_HEADER}: 1 header"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.startswith("/static/"):
            # Revalidate (ETag) every load so an upgraded server never runs a stale app.js.
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=static_dir()), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir() / "index.html")

    @app.get("/api/providers", response_model=list[ProviderInfo])
    def providers() -> list[dict[str, Any]]:
        return service.list_providers()

    @app.get("/api/search", response_model=SearchResponse)
    def search(
        q: str,
        kind: str = "video",
        limit: int = 20,
        commercial_only: bool = False,
        orientation: str | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        provider: str | None = None,
        no_cache: bool = False,
        style: str = "any",
        expand: bool = True,
        related: bool = True,
        cleared_only: bool = False,
        safe: bool | None = None,
        shorts: bool = False,
    ) -> SearchResponse:
        try:
            asset_kind = Kind(kind)
        except ValueError:
            valid = ", ".join(k.value for k in Kind)
            raise HTTPException(422, f"invalid kind {kind!r}; choose one of: {valid}") from None
        if orientation == "":
            orientation = None
        if orientation is not None and orientation not in ORIENTATIONS:
            valid = ", ".join(ORIENTATIONS)
            raise HTTPException(422, f"invalid orientation {orientation!r}; choose one of: {valid}")
        if style not in STYLES:
            valid = ", ".join(STYLES)
            raise HTTPException(422, f"invalid style {style!r}; choose one of: {valid}")
        if limit < 1:
            raise HTTPException(422, "limit must be at least 1")
        filters = SearchFilters(
            kind=asset_kind,
            commercial_only=commercial_only,
            min_duration=min_duration,
            max_duration=max_duration,
            orientation=orientation,  # type: ignore[arg-type]
            style=style,  # type: ignore[arg-type]
            expand=expand,
            related=related,
            safe=safesearch.resolve_safe(safe),  # None: the [search] safe default
            shorts=shorts,
        )
        try:
            outcome = service.search_expanded(
                q,
                filters,
                provider=provider or None,
                use_cache=not no_cache,
                limit=limit,
                cleared_only=cleared_only,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc.args[0])) from None
        return SearchResponse(
            assets=_attach_history([_asset_out(asset) for asset in outcome.assets]),
            errors=[SearchErrorOut(provider=e.provider, message=e.message) for e in outcome.errors],
            linkouts=linkouts_for(asset_kind),
            expanded_queries=outcome.expanded_queries,
            related=_attach_history([_asset_out(asset) for asset in outcome.related]),
            related_kind=outcome.related_kind.value if outcome.related_kind else None,
            filtered_count=outcome.filtered_count,
            relaxed_query=outcome.relaxed_query,
        )

    @app.get("/api/recall", response_model=RecallResponse)
    def recall_suggestions(
        q: str = "", kind: str | None = None, online: bool = False, safe: bool | None = None
    ) -> RecallResponse:
        # Offline and cheap (no network) by default: the UI may call it while the user types.
        # ``online=true`` also asks Know Your Meme (throttled, cached 7 days); a failed or
        # disabled lookup just leaves the offline suggestions. It never waits on the KYM
        # sitemap (a missing one is built in the background). ``safe`` None: the [search] safe
        # default; sensitive KYM entries are fuzzy candidates only with safe off.
        asset_kind: Kind | None = None
        if kind:
            try:
                asset_kind = Kind(kind)
            except ValueError:
                valid = ", ".join(k.value for k in Kind)
                raise HTTPException(422, f"invalid kind {kind!r}; choose one of: {valid}") from None
        suggestions = recall.expand_query(q, asset_kind)
        if online and q.strip():
            suggestions += [
                name
                for name in service.online_recall_names(
                    q, asset_kind, safe=safesearch.resolve_safe(safe)
                )
                if name not in suggestions
            ]
        return RecallResponse(suggestions=suggestions)

    @app.get("/api/assist/status", response_model=AssistStatusOut)
    def assist_status() -> AssistStatusOut:
        # Availability only (e.g. `claude --version`); never an LLM call.
        statuses = assist.available_backends()
        return AssistStatusOut(
            backends=[AssistBackendOut(**s.model_dump()) for s in statuses],
            configured=assist.configured_backend(),
            auto_pick=assist.auto_pick(statuses),
            enabled=assist.configured_enabled(),
        )

    @app.post("/api/assist", response_model=AssistOut)
    async def assist_describe(body: AssistIn) -> AssistOut:
        # The only place the web app calls an LLM: an explicit user request, assist enabled.
        if not body.text.strip():
            raise HTTPException(422, "text must not be empty")
        asset_kind: Kind | None = None
        if body.kind:
            try:
                asset_kind = Kind(body.kind)
            except ValueError:
                valid = ", ".join(k.value for k in Kind)
                raise HTTPException(
                    422, f"invalid kind {body.kind!r}; choose one of: {valid}"
                ) from None
        style = body.style or None
        if style is not None and style not in STYLES:
            valid = ", ".join(STYLES)
            raise HTTPException(422, f"invalid style {style!r}; choose one of: {valid}")
        backend = body.backend or None
        if backend is not None and backend not in assist.BACKEND_CHOICES:
            valid = ", ".join(assist.BACKEND_CHOICES)
            raise HTTPException(422, f"invalid backend {backend!r}; choose one of: {valid}")
        timeout_s = assist.configured_timeout() + ASSIST_TIMEOUT_GRACE_S
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    assist.describe, body.text, kind=asset_kind, style=style, backend=backend
                ),
                timeout=timeout_s,
            )
        except assist.AssistDisabledError as exc:
            raise HTTPException(403, str(exc)) from None
        except assist.AssistUnavailableError as exc:
            raise HTTPException(503, str(exc)) from None
        except assist.AssistError as exc:
            raise HTTPException(502, str(exc)) from None
        except TimeoutError:
            raise HTTPException(502, f"assist timed out after {timeout_s:g}s") from None
        return AssistOut(
            queries=result.queries,
            names=result.names,
            kind_hint=result.kind_hint.value if result.kind_hint else None,
            notes=result.notes,
            rights_note=result.rights_note,
            backend=result.backend,
            model=result.model,
            cost_usd=result.cost_usd,
        )

    @app.get("/api/phrase", response_model=PhraseResponse)
    def phrase(
        q: str = "",
        source: Annotated[list[str] | None, Query()] = None,
        within: str | None = None,
        max_videos: int = 10,
        limit: int = 20,
        max_per_video: int = 3,
        no_cache: bool = False,
        safe: bool | None = None,
    ) -> PhraseResponse:
        # A sync route: FastAPI runs it in its threadpool, like /api/search (network work).
        if not q.strip():
            raise HTTPException(422, "q must not be empty")
        for name in source or []:
            if name not in service.PHRASE_SOURCES:
                valid = ", ".join(service.PHRASE_SOURCES)
                raise HTTPException(404, f"unknown phrase source {name!r}; choose from: {valid}")
        if not 1 <= max_videos <= PHRASE_MAX_VIDEOS:
            raise HTTPException(422, f"max_videos must be between 1 and {PHRASE_MAX_VIDEOS}")
        if not 1 <= limit <= PHRASE_MAX_LIMIT:
            raise HTTPException(422, f"limit must be between 1 and {PHRASE_MAX_LIMIT}")
        if not 0 <= max_per_video <= PHRASE_MAX_PER_VIDEO:
            raise HTTPException(422, f"max_per_video must be between 0 and {PHRASE_MAX_PER_VIDEO}")
        try:
            outcome = service.phrase_search(
                q.strip(),
                sources=source or None,
                within=(within or "").strip() or None,
                max_videos=max_videos,
                limit=limit,
                use_cache=not no_cache,
                max_per_video=max_per_video,
                safe=safe,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc.args[0])) from None
        except service.CacheUnavailableError as exc:
            raise HTTPException(503, str(exc)) from None
        return PhraseResponse(
            hits=[
                PhraseHitOut(
                    asset=_asset_out(hit.asset),
                    start_s=hit.start_s,
                    end_s=hit.end_s,
                    text=hit.text,
                    lang=hit.lang,
                    auto_generated=hit.auto_generated,
                    ref=hit.ref,
                    clip=hit.clip_spec(),
                )
                for hit in outcome.hits
            ],
            errors=[SearchErrorOut(provider=e.provider, message=e.message) for e in outcome.errors],
            linkouts=[link for link in linkouts_for(Kind.VIDEO) if link.name == PHRASE_LINKOUT],
            filtered_count=outcome.filtered_count,
        )

    @app.get("/api/preview/{provider}/{asset_id:path}")
    def preview(provider: str, asset_id: str) -> Response:
        _, asset = _cached_asset(provider, asset_id)
        return _proxy_stream(asset.preview_url, "preview")

    @app.get("/api/media/{provider}/{asset_id:path}")
    def media(provider: str, asset_id: str, request: Request) -> Response:
        source, asset = _cached_asset(provider, asset_id)
        url = asset.media_url
        if url is None and source.id == MEDIA_RESOLVE_PROVIDER:
            try:
                plan = source.resolve(asset)
            except ProviderError as exc:
                raise HTTPException(502, f"media resolve failed: {exc}") from None
            if plan.method == "http":
                url = plan.url
        range_header = request.headers.get("range")
        return _proxy_stream(
            url,
            "media",
            request_headers={"Range": range_header} if range_header else None,
            forward=MEDIA_FORWARD_HEADERS,
        )

    @app.post("/api/download", response_model=DownloadAccepted, status_code=202)
    def download(body: DownloadRequest, request: Request) -> dict[str, Any] | JSONResponse:
        if (body.ref is None) == (body.url is None):
            raise HTTPException(400, "send exactly one of ref or url")
        if body.url is not None and not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "url must start with http:// or https://")
        if body.max_wall_s is not None and body.max_wall_s > CLIP_MAX_WALL_S_CAP:
            raise HTTPException(400, f"max_wall_s above {CLIP_MAX_WALL_S_CAP:g}")
        title = body.title
        if body.ref is not None:
            try:
                provider, asset = service.resolve_ref(body.ref)
            except service.RefError as exc:
                return JSONResponse({"detail": str(exc), "hint": exc.hint}, status_code=400)
            except service.CacheUnavailableError as exc:
                raise HTTPException(503, str(exc)) from None
            ytdlp = provider.id == YTDLP_ONLY_PROVIDER
            # audio assets go through: download_ref accepts direct mp3s, rejects the rest
            if body.mp3 and not ytdlp and asset.kind not in recall.AUDIO_KINDS:
                raise HTTPException(400, "mp3 only applies to yt-dlp sources")
            if body.clip and not ytdlp and asset.kind not in service.CLIP_KINDS:
                raise HTTPException(400, "clip only applies to video/audio sources")
            title = asset.title or title
        try:
            clip = parse_clip(body.clip) if body.clip else None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        job = request.app.state.jobs.submit(
            ref=body.ref,
            url=body.url,
            mp3=body.mp3,
            clip=clip,
            max_wall_s=body.max_wall_s,
            quality=body.quality,
            title=title,
        )
        return {"job_id": job["job_id"], "status": job["status"]}

    @app.get("/api/jobs", response_model=list[JobOut])
    def jobs(request: Request) -> list[dict[str, Any]]:
        return request.app.state.jobs.list()

    @app.get("/api/jobs/{job_id}", response_model=JobOut)
    def job(job_id: str, request: Request) -> dict[str, Any]:
        found = request.app.state.jobs.get(job_id)
        if found is None:
            raise HTTPException(404, f"unknown job {job_id!r}")
        return found

    @app.get("/api/history", response_model=list[HistoryRow])
    def history(limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1:
            raise HTTPException(422, "limit must be at least 1")
        try:
            return service.history(limit)
        except service.CacheUnavailableError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.get("/api/library/{history_id}/file")
    def library_file(history_id: int) -> FileResponse:
        return _library_file(history_id, "path")

    @app.get("/api/library/{history_id}/sidecar")
    def library_sidecar(history_id: int) -> FileResponse:
        return _library_file(history_id, "sidecar")

    @app.post("/api/library/{history_id}/keyout", response_model=JobOut, status_code=202)
    def library_keyout(history_id: int, body: KeyOutRequest, request: Request) -> dict[str, Any]:
        target = _library_path(history_id, "path")
        if target.suffix.lower() not in service.KEYOUT_EXTS:
            raise HTTPException(400, f"cannot key out {target.name}: not a video file")
        if target.name.lower().endswith(service.KEYED_SUFFIX):
            raise HTTPException(
                400,
                f"cannot key out {target.name}: already a keyed export ({service.KEYED_SUFFIX})",
            )
        if body.max_wall_s is not None and body.max_wall_s > CLIP_MAX_WALL_S_CAP:
            raise HTTPException(400, f"max_wall_s above {CLIP_MAX_WALL_S_CAP:g}")
        return request.app.state.jobs.submit_keyout(history_id, body.tolerance, body.max_wall_s)

    @app.get("/api/settings", response_model=SettingsOut)
    def settings() -> SettingsOut:
        return _settings_out()

    @app.put("/api/settings/assist", response_model=SettingsOut)
    def put_assist_settings(body: AssistSettingsIn) -> SettingsOut:
        if body.backend is not None and body.backend not in assist.BACKEND_CHOICES:
            valid = ", ".join(assist.BACKEND_CHOICES)
            raise HTTPException(422, f"invalid backend {body.backend!r}; choose one of: {valid}")
        if body.enabled is not None:
            config.set_setting("assist", "enabled", body.enabled)
        if body.backend is not None:
            # "auto" is the default: drop the key rather than pin it in config.toml.
            config.set_setting(
                "assist", "backend", None if body.backend == "auto" else body.backend
            )
        if body.model is not None:
            config.set_setting("assist", "model", body.model.strip() or None)
        for backend in assist.BACKENDS:
            value = getattr(body, f"{backend}_model")
            if value is not None:
                config.set_setting("assist", f"{backend}_model", value.strip() or None)
        return _settings_out()

    @app.put("/api/settings/search", response_model=SettingsOut)
    def put_search_settings(body: SearchSettingsIn) -> SettingsOut:
        # None removes the key, so safe search falls back to its default (on).
        config.set_setting("search", "safe", body.safe)
        return _settings_out()

    @app.put("/api/settings/keys/{provider_id}", response_model=KeyOut)
    def put_key(provider_id: str, body: KeyIn) -> KeyOut:
        if provider_id not in ASSIST_KEY_IDS and provider_id not in _provider_ids():
            raise HTTPException(404, f"unknown provider {provider_id!r}")
        value = body.key.strip()
        config.set_key(provider_id, value or None)
        key = config.get_key(provider_id)
        return KeyOut(provider=provider_id, key=config.mask_key(key) if key else None)

    @app.put("/api/settings/library_dir", response_model=LibraryDirOut)
    def put_library_dir(body: LibraryDirIn) -> LibraryDirOut:
        if not body.dir.strip() or not Path(body.dir).is_absolute():
            raise HTTPException(400, f"library dir must be an absolute path, got {body.dir!r}")
        config.set_setting("library", "dir", str(Path(body.dir)))
        return LibraryDirOut(library_dir=str(config.library_dir()))

    @app.get("/api/doctor", response_model=DoctorOut)
    def doctor() -> DoctorOut:
        def tool(name: str) -> ToolInfo:
            path = binaries.find_tool(name)
            return ToolInfo(found=path is not None, path=str(path) if path else None)

        return DoctorOut(
            ffmpeg=tool("ffmpeg"),
            deno=tool("deno"),
            yt_dlp=YtDlpInfo(version=binaries.ytdlp_version()),
            ejs=binaries.has_ejs(),
        )

    @app.post("/api/open", response_model=OpenOut)
    def open_folder(body: OpenIn) -> OpenOut:
        library = config.library_dir().resolve()
        target = Path(body.path).resolve()
        if not target.is_relative_to(library):
            raise HTTPException(403, "path is outside the library folder")
        if not target.exists():
            raise HTTPException(404, "path does not exist")
        folder = target if target.is_dir() else target.parent
        open_in_file_manager(folder)
        return OpenOut(opened=str(folder))

    return app
