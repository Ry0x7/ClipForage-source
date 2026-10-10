"""SQLite ``cache.db`` in the user data dir: per-provider-TTL search cache + download history.

Also a small generic TTL blob cache (``get_blob`` / ``set_blob``) for online lookups.
"""

import hashlib
import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import TypeAdapter

from clipforage.config import data_dir
from clipforage.models import Asset, Cue, SearchFilters

DEFAULT_TTL_S = 3600
EMPTY_RESULT_TTL_S = 120
"""An empty cached search result older than this is treated as a miss."""
CAPTIONS_TTL_S = 7 * 86400
"""How long fetched captions count as fresh before phrase search refetches them."""

_ASSETS = TypeAdapter(list[Asset])

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    stored_at REAL NOT NULL,
    ttl_s INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    path TEXT NOT NULL,
    sidecar TEXT NOT NULL,
    provider TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    title TEXT NOT NULL,
    license TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    extra TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS download_history_asset
    ON download_history (provider, asset_id);
CREATE TABLE IF NOT EXISTS blob_cache (
    key TEXT PRIMARY KEY,
    stored_at REAL NOT NULL,
    ttl_s INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""


_CAPTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS caption_sources (
    provider TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    lang TEXT,
    auto INTEGER NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_s INTEGER NOT NULL,
    asset_json TEXT NOT NULL,
    PRIMARY KEY (provider, asset_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS captions USING fts5(
    text,
    provider UNINDEXED,
    asset_id UNINDEXED,
    start_s UNINDEXED,
    end_s UNINDEXED,
    span UNINDEXED
);
"""
CAPTION_WINDOW_MAX = 3
"""``put_captions`` indexes windows of 1..this many consecutive cues."""
_JOINED_FETCH_FACTOR = 4
"""``search_captions`` over-fetches this many rows per wanted hit before dropping wide rows."""


class CaptionIndexUnavailableError(sqlite3.OperationalError):
    """The caption index needs SQLite FTS5, which this Python's sqlite3 build lacks."""


@dataclass(frozen=True)
class CaptionRow:
    """One caption cue matching a phrase query, with its source asset."""

    provider: str
    asset_id: str
    start_s: float
    end_s: float
    text: str
    asset: Asset
    lang: str | None
    auto_generated: bool
    span: int = 1
    """Number of consecutive cues joined in this row (1..CAPTION_WINDOW_MAX)."""

    @property
    def joined(self) -> bool:
        """True for a row that joins several consecutive cues (a phrase split across lines)."""
        return self.span > 1


def fts_phrase(query: str) -> str:
    """Quote ``query`` as one FTS5 phrase (existing double quotes are dropped); "" if blank."""
    words = query.replace('"', " ").split()
    return f'"{" ".join(words)}"' if words else ""


def _filter_caption_rows(rows: list[tuple[Any, ...]], max_per_asset: int) -> list[tuple[Any, ...]]:
    """Drop rows covering a narrower match, and exact duplicates; cap rows per asset (rank kept).

    Rows are ``(provider, asset_id, start_s, end_s, text, span, asset_json, lang, auto)``.
    A row is dropped when a matching row of the same asset with a smaller span lies inside
    its range, so the narrowest window containing the phrase wins.
    """
    norm = [
        (p, a, float(start), float(end), text, int(span), *rest)
        for p, a, start, end, text, span, *rest in rows
    ]
    ranges: dict[tuple[str, str], list[tuple[int, float, float]]] = {}
    for p, a, start, end, _text, span, *_ in norm:
        ranges.setdefault((p, a), []).append((span, start, end))
    seen: set[tuple[str, str, float, float, str]] = set()
    per_asset: dict[tuple[str, str], int] = {}
    out: list[tuple[Any, ...]] = []
    for row in norm:
        p, a, start, end, text, span = row[:6]
        if span > 1 and any(
            o_span < span and start <= o_start and o_end <= end
            for o_span, o_start, o_end in ranges[(p, a)]
        ):
            continue
        key = (p, a, start, end, text)
        if key in seen:
            continue
        seen.add(key)
        if max_per_asset > 0:
            if per_asset.get((p, a), 0) >= max_per_asset:
                continue
            per_asset[(p, a)] = per_asset.get((p, a), 0) + 1
        out.append(row)
    return out


def db_path() -> Path:
    """Return the path of ``cache.db`` in the user data dir."""
    return data_dir() / "cache.db"


def make_key(provider_id: str, query: str, filters: SearchFilters) -> str:
    """Return the cache key for one provider search (sha256 hex)."""
    raw = provider_id + "\n" + query + "\n" + filters.model_dump_json()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Cache:
    """Search cache and download history backed by SQLite."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._captions_ready = False
        self._rebuilt_caption_sources = 0
        try:
            with self._conn:
                self._conn.executescript(_SCHEMA)
        except sqlite3.Error:
            self._conn.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def get_search(
        self,
        provider_id: str,
        query: str,
        filters: SearchFilters,
        now: float | None = None,
        *,
        allow_empty: bool = False,
    ) -> list[Asset] | None:
        """Return cached assets, or None on a miss or expired entry (which is deleted).

        Unless ``allow_empty`` (providers with ``cache_empty_results = True``), an empty cached
        result counts as expired after ``EMPTY_RESULT_TTL_S`` whatever its TTL (entries written
        before empty results stopped being cached).
        """
        now = time.time() if now is None else now
        key = make_key(provider_id, query, filters)
        row = self._conn.execute(
            "SELECT stored_at, ttl_s, payload FROM search_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        stored_at, ttl_s, payload = row
        assets = _ASSETS.validate_json(payload) if now - stored_at <= ttl_s else None
        # An empty result is often a transient provider hiccup: never trust it for long.
        stale_empty = not allow_empty and not assets and now - stored_at > EMPTY_RESULT_TTL_S
        if assets is None or stale_empty:
            with self._conn:
                self._conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
            return None
        return assets

    def put_search(
        self,
        provider_id: str,
        query: str,
        filters: SearchFilters,
        assets: list[Asset],
        ttl_s: int,
        now: float | None = None,
    ) -> None:
        """Store a provider's raw search results with its TTL (replaces any old entry)."""
        now = time.time() if now is None else now
        key = make_key(provider_id, query, filters)
        payload = _ASSETS.dump_json(assets).decode("utf-8")
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO search_cache (key, provider, stored_at, ttl_s, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, provider_id, now, ttl_s, payload),
            )

    def find_asset(self, provider_id: str, asset_id: str, now: float | None = None) -> Asset | None:
        """Return the newest unexpired cached asset with this provider and id, else None."""
        now = time.time() if now is None else now
        rows = self._conn.execute(
            "SELECT stored_at, ttl_s, payload FROM search_cache WHERE provider = ? "
            "ORDER BY stored_at DESC",
            (provider_id,),
        ).fetchall()
        for stored_at, ttl_s, payload in rows:
            if now - stored_at > ttl_s:
                continue
            for asset in _ASSETS.validate_json(payload):
                if asset.id == asset_id:
                    return asset
        return None

    def get_blob(self, key: str, now: float | None = None) -> str | None:
        """Return a stored text payload, or None on a miss or expired entry (which is deleted)."""
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT stored_at, ttl_s, payload FROM blob_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        stored_at, ttl_s, payload = row
        if now - stored_at > ttl_s:
            with self._conn:
                self._conn.execute("DELETE FROM blob_cache WHERE key = ?", (key,))
            return None
        return str(payload)

    def set_blob(self, key: str, payload: str, ttl_s: int, now: float | None = None) -> None:
        """Store a text payload under ``key`` with its TTL (replaces any old entry)."""
        now = time.time() if now is None else now
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO blob_cache (key, stored_at, ttl_s, payload) "
                "VALUES (?, ?, ?, ?)",
                (key, now, ttl_s, payload),
            )

    def clear_search(self) -> int:
        """Delete all cached searches and return how many rows were removed."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM search_cache")
        return cur.rowcount

    def record_download(
        self,
        *,
        url: str,
        path: Path,
        sidecar: Path,
        asset: Asset,
        fetched_at: datetime,
        extra: dict[str, Any] | None = None,
    ) -> int:
        """Append a completed download to the history and return the new row id."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO download_history (url, path, sidecar, provider, asset_id, title, "
                "license, fetched_at, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    str(path),
                    str(sidecar),
                    asset.provider,
                    asset.id,
                    asset.title,
                    asset.license,
                    fetched_at.isoformat(),
                    json.dumps(extra or {}, ensure_ascii=False),
                ),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return up to ``limit`` downloads, newest first."""
        return self._history_rows("ORDER BY id DESC LIMIT ?", (limit,))

    def get_download(self, history_id: int) -> dict[str, Any] | None:
        """Return one history row (same shape as ``history()`` rows) by id, or None."""
        rows = self._history_rows("WHERE id = ?", (history_id,))
        return rows[0] if rows else None

    def latest_download_for_path(self, path: Path) -> dict[str, Any] | None:
        """Return the newest history row whose file is ``path``, or None."""
        rows = self._history_rows("WHERE path = ? ORDER BY id DESC LIMIT 1", (str(path),))
        return rows[0] if rows else None

    def latest_download_for_asset(self, provider: str, asset_id: str) -> dict[str, Any] | None:
        """Return the newest history row for ``provider``/``asset_id``, or None.

        The file may no longer exist; callers check that themselves.
        """
        rows = self._history_rows(
            "WHERE provider = ? AND asset_id = ? ORDER BY id DESC LIMIT 1", (provider, asset_id)
        )
        return rows[0] if rows else None

    def _history_rows(self, clause: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT id, url, path, sidecar, provider, asset_id, title, license, fetched_at, extra "
            f"FROM download_history {clause}",
            params,
        )
        cols = [c[0] for c in cur.description]
        rows = []
        for values in cur.fetchall():
            row = dict(zip(cols, values, strict=True))
            row["extra"] = json.loads(row["extra"])
            rows.append(row)
        return rows

    def _ensure_captions(self) -> None:
        """Create the caption tables on first use; raise CaptionIndexUnavailableError w/o FTS5."""
        if self._captions_ready:
            return
        try:
            with self._conn:
                self._conn.executescript(_CAPTIONS_SCHEMA)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise CaptionIndexUnavailableError(
                    f"phrase search needs SQLite FTS5, which this Python lacks ({exc})"
                ) from exc
            raise
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(captions)")}
        if "span" not in columns:
            # Index from before span rows (pre-joined or joined-era): rebuild; refetch captions.
            with self._conn:
                self._conn.execute("DROP TABLE captions")
                cur = self._conn.execute("DELETE FROM caption_sources")
                self._rebuilt_caption_sources += max(cur.rowcount, 0)
                self._conn.executescript(_CAPTIONS_SCHEMA)
        self._captions_ready = True

    def has_captions(self, provider: str, asset_id: str, now: float | None = None) -> bool:
        """Return True if captions (possibly an empty set) for the asset are stored and fresh."""
        self._ensure_captions()
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT fetched_at, ttl_s FROM caption_sources WHERE provider = ? AND asset_id = ?",
            (provider, asset_id),
        ).fetchone()
        return row is not None and now - row[0] <= row[1]

    def put_captions(
        self,
        asset: Asset,
        cues: Sequence[Cue],
        *,
        lang: str | None,
        auto_generated: bool,
        ttl_s: int = CAPTIONS_TTL_S,
        now: float | None = None,
    ) -> None:
        """Replace the asset's indexed cues (an empty list is stored too, to skip refetches)."""
        self._ensure_captions()
        now = time.time() if now is None else now
        key = (asset.provider, asset.id)
        with self._conn:
            self._conn.execute("DELETE FROM captions WHERE provider = ? AND asset_id = ?", key)
            self._conn.execute(
                "INSERT OR REPLACE INTO caption_sources (provider, asset_id, lang, auto, "
                "fetched_at, ttl_s, asset_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*key, lang, int(auto_generated), now, ttl_s, asset.model_dump_json()),
            )
            # One row per window of 1..CAPTION_WINDOW_MAX consecutive cues, so a phrase split
            # across up to that many caption lines is still found (span = cues in the row).
            windows = [
                (
                    " ".join(cue.text for cue in cues[i : i + span]),
                    *key,
                    cues[i].start_s,
                    cues[i + span - 1].end_s,
                    span,
                )
                for span in range(1, CAPTION_WINDOW_MAX + 1)
                for i in range(len(cues) - span + 1)
            ]
            self._conn.executemany(
                "INSERT INTO captions (text, provider, asset_id, start_s, end_s, span) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                windows,
            )

    def search_captions(
        self,
        query: str,
        *,
        providers: Sequence[str] | None = None,
        limit: int = 20,
        max_per_asset: int = 0,
    ) -> list[CaptionRow]:
        """Return cues containing ``query`` as a phrase, best match (bm25) first.

        Searches every indexed asset, expired or not; ``providers`` restricts the sources.
        A multi-cue row is dropped when a matching row of the same asset with a smaller span
        lies inside its range (narrowest window wins); exact duplicates are dropped. ``max_per_asset`` (0 = no cap)
        limits hits per asset, applied before ``limit``.
        """
        self._ensure_captions()
        phrase = fts_phrase(query)
        if not phrase or limit < 1:
            return []
        sql = (
            "SELECT c.provider, c.asset_id, c.start_s, c.end_s, c.text, c.span, s.asset_json, "
            "s.lang, s.auto FROM captions AS c JOIN caption_sources AS s "
            "ON s.provider = c.provider AND s.asset_id = c.asset_id WHERE captions MATCH ?"
        )
        params: list[Any] = [phrase]
        if providers is not None:
            if not providers:
                return []
            sql += f" AND c.provider IN ({', '.join('?' for _ in providers)})"
            params.extend(providers)
        sql += " ORDER BY bm25(captions), CAST(c.start_s AS REAL) LIMIT ?"
        fetch = limit * _JOINED_FETCH_FACTOR
        while True:
            rows = self._conn.execute(sql, [*params, fetch]).fetchall()
            hits = _filter_caption_rows(rows, max_per_asset)
            if len(hits) >= limit or len(rows) < fetch:
                break
            fetch *= 4  # too many rows were dropped; look further down the ranking
        assets: dict[str, Asset] = {}
        return [
            CaptionRow(
                provider=provider,
                asset_id=asset_id,
                start_s=start_s,
                end_s=end_s,
                text=text,
                asset=assets.setdefault(asset_json, Asset.model_validate_json(asset_json)),
                lang=lang,
                auto_generated=bool(auto),
                span=int(span),
            )
            for provider, asset_id, start_s, end_s, text, span, asset_json, lang, auto in (
                hits[:limit]
            )
        ]

    def clear_captions(self) -> int:
        """Delete every indexed caption set; return how many assets were removed."""
        self._ensure_captions()
        with self._conn:
            cur = self._conn.execute("DELETE FROM caption_sources")
            self._conn.execute("DELETE FROM captions")
        # Rows dropped by an old-schema rebuild count too, but only in this Cache instance:
        # they are reported by the first clear_captions() after whatever call triggered it.
        removed = self._rebuilt_caption_sources + cur.rowcount
        self._rebuilt_caption_sources = 0
        return removed
