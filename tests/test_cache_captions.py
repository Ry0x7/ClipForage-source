import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Self

import pytest

from clipforage.cache import (
    CAPTIONS_TTL_S,
    Cache,
    CaptionIndexUnavailableError,
    CaptionRow,
    fts_phrase,
)
from clipforage.models import Asset, Cue, Kind


def make_asset(provider: str, asset_id: str) -> Asset:
    return Asset(
        provider=provider,
        id=asset_id,
        kind=Kind.VIDEO,
        title=f"Title {asset_id}",
        source_url=f"https://example.com/{asset_id}",
        license="unknown",
        attribution_text="attr",
        commercial_ok=None,
    )


CUES = [
    Cue(start_s=10.0, end_s=12.0, text="never gonna give you up"),
    Cue(start_s=12.0, end_s=14.0, text="never gonna let you down"),
    Cue(start_s=20.0, end_s=22.5, text="give you up? never!"),
]


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[Cache]:
    with Cache(tmp_path / "cache.db") as c:
        yield c


def test_put_has_and_search(cache: Cache) -> None:
    asset = make_asset("youtube", "vid1")
    assert cache.has_captions("youtube", "vid1") is False
    cache.put_captions(asset, CUES, lang="en", auto_generated=True)
    assert cache.has_captions("youtube", "vid1") is True
    rows = cache.search_captions("Give You UP")
    assert {(r.start_s, r.text) for r in rows} == {
        (10.0, "never gonna give you up"),
        (20.0, "give you up? never!"),
    }
    row = rows[0]
    assert isinstance(row, CaptionRow)
    assert row.provider == "youtube"
    assert row.asset_id == "vid1"
    assert row.asset == asset
    assert row.lang == "en"
    assert row.auto_generated is True


def test_search_is_a_phrase_not_bag_of_words(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "v"), CUES, lang="en", auto_generated=False)
    assert cache.search_captions("you give") == []
    assert [r.text for r in cache.search_captions("let you down")] == ["never gonna let you down"]


def test_put_replaces_existing_rows(cache: Cache) -> None:
    asset = make_asset("youtube", "v")
    cache.put_captions(asset, CUES, lang="en", auto_generated=True)
    cache.put_captions(
        asset, [Cue(start_s=1.0, end_s=2.0, text="brand new line")], lang=None, auto_generated=False
    )
    assert cache.search_captions("never gonna") == []
    rows = cache.search_captions("brand new")
    assert len(rows) == 1
    assert rows[0].lang is None
    assert rows[0].auto_generated is False


def test_empty_cue_list_is_stored(cache: Cache) -> None:
    cache.put_captions(make_asset("internet_archive", "film"), [], lang=None, auto_generated=False)
    assert cache.has_captions("internet_archive", "film") is True
    assert cache.search_captions("anything") == []


def test_ttl_expiry(cache: Cache) -> None:
    asset = make_asset("youtube", "v")
    cache.put_captions(asset, CUES, lang="en", auto_generated=True, now=1000.0)
    assert cache.has_captions("youtube", "v", now=1000.0 + CAPTIONS_TTL_S) is True
    assert cache.has_captions("youtube", "v", now=1000.0 + CAPTIONS_TTL_S + 1) is False
    cache.put_captions(asset, CUES, lang="en", auto_generated=True, ttl_s=5, now=0.0)
    assert cache.has_captions("youtube", "v", now=6.0) is False
    # Expired captions stay searchable until refetched or cleared.
    assert len(cache.search_captions("never gonna give")) == 1
    assert CAPTIONS_TTL_S == 7 * 86400


def test_provider_filter_and_limit(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "a"), CUES, lang="en", auto_generated=True)
    cache.put_captions(make_asset("internet_archive", "b"), CUES, lang="en", auto_generated=False)
    assert len(cache.search_captions("never gonna")) == 4
    ia = cache.search_captions("never gonna", providers=["internet_archive"])
    assert {r.provider for r in ia} == {"internet_archive"}
    assert len(ia) == 2
    assert cache.search_captions("never gonna", providers=[]) == []
    assert len(cache.search_captions("never gonna", limit=3)) == 3
    assert cache.search_captions("never gonna", limit=0) == []


def test_ordering_bm25_then_start(cache: Cache) -> None:
    cues = [
        Cue(start_s=1.0, end_s=2.0, text="well hello there my old friend how are you doing today"),
        Cue(start_s=5.0, end_s=6.0, text="hello"),
        Cue(start_s=30.0, end_s=31.0, text="hello"),
    ]
    cache.put_captions(make_asset("youtube", "v"), cues, lang="en", auto_generated=True)
    rows = cache.search_captions("hello")
    assert [r.start_s for r in rows] == [5.0, 30.0, 1.0]
    assert not any(r.joined for r in rows)  # multi-cue rows covering single hits are dropped


SPLIT = [
    Cue(start_s=10.0, end_s=12.0, text="good morning everyone welcome back"),
    Cue(start_s=12.0, end_s=14.5, text="today we're making coffee"),
    Cue(start_s=15.0, end_s=16.0, text="the end"),
]


def test_phrase_split_across_two_cues_is_found(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "v"), SPLIT, lang="en", auto_generated=True)
    rows = cache.search_captions("welcome back today we're")
    assert [(r.start_s, r.end_s, r.span, r.joined) for r in rows] == [(10.0, 14.5, 2, True)]
    assert rows[0].text == "good morning everyone welcome back today we're making coffee"


def test_phrase_within_one_line_returns_only_the_single_line(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "v"), SPLIT, lang="en", auto_generated=True)
    rows = cache.search_captions("making coffee")
    assert [(r.start_s, r.end_s, r.span, r.joined, r.text) for r in rows] == [
        (12.0, 14.5, 1, False, "today we're making coffee")
    ]


FOUR = [
    Cue(start_s=1.0, end_s=2.0, text="so the thing is"),
    Cue(start_s=2.0, end_s=3.0, text="we never"),
    Cue(start_s=3.0, end_s=4.5, text="really"),
    Cue(start_s=5.0, end_s=6.0, text="knew it okay"),
]


def test_phrase_split_across_three_cues_is_found(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "v"), FOUR, lang="en", auto_generated=True)
    rows = cache.search_captions("we never really knew")
    assert [(r.start_s, r.end_s, r.span) for r in rows] == [(2.0, 6.0, 3)]
    assert rows[0].text == "we never really knew it okay"
    rows = cache.search_captions("thing is we never really")
    assert [(r.start_s, r.end_s, r.span, r.text) for r in rows] == [
        (1.0, 4.5, 3, "so the thing is we never really")
    ]


def test_phrase_within_two_cues_returns_only_the_span_two_row(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "v"), FOUR, lang="en", auto_generated=True)
    rows = cache.search_captions("never really")
    # Span-3 rows (1.0-4.5 and 2.0-6.0) also contain the phrase; the narrowest window wins.
    assert [(r.start_s, r.end_s, r.span, r.text) for r in rows] == [
        (2.0, 4.5, 2, "we never really")
    ]


def test_phrase_spanning_four_cues_is_not_found(cache: Cache) -> None:
    # Documented limit: windows cover at most CAPTION_WINDOW_MAX (3) consecutive cues.
    cache.put_captions(make_asset("youtube", "v"), FOUR, lang="en", auto_generated=True)
    assert cache.search_captions("thing is we never really knew") == []


def test_exact_duplicates_are_dropped(cache: Cache) -> None:
    cues = [Cue(start_s=1.0, end_s=2.0, text="hey"), Cue(start_s=1.0, end_s=2.0, text="hey")]
    cache.put_captions(make_asset("youtube", "v"), cues, lang="en", auto_generated=True)
    assert [(r.start_s, r.joined) for r in cache.search_captions("hey")] == [(1.0, False)]


def test_max_per_asset_cap_before_limit(cache: Cache) -> None:
    many = [Cue(start_s=float(i * 10), end_s=float(i * 10 + 1), text="hello") for i in range(6)]
    cache.put_captions(make_asset("youtube", "a"), many, lang="en", auto_generated=True)
    cache.put_captions(make_asset("youtube", "b"), many[:2], lang="en", auto_generated=True)
    rows = cache.search_captions("hello", max_per_asset=3, limit=20)
    counts = {a: sum(r.asset_id == a for r in rows) for a in ("a", "b")}
    assert counts == {"a": 3, "b": 2}
    assert len(cache.search_captions("hello", max_per_asset=0, limit=20)) == 8
    assert len(cache.search_captions("hello", max_per_asset=3, limit=4)) == 4


def test_old_index_without_joined_column_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE caption_sources (provider TEXT, asset_id TEXT, lang TEXT, auto INTEGER, "
        "fetched_at REAL, ttl_s INTEGER, asset_json TEXT, PRIMARY KEY(provider, asset_id));"
        "CREATE VIRTUAL TABLE captions USING fts5(text, provider UNINDEXED, "
        "asset_id UNINDEXED, start_s UNINDEXED, end_s UNINDEXED);"
        "INSERT INTO caption_sources VALUES ('youtube', 'v', 'en', 1, 1e12, 999, '{}');"
    )
    conn.commit()
    conn.close()
    with Cache(path) as cache:
        assert cache.has_captions("youtube", "v") is False  # refetched after the rebuild
        cache.put_captions(make_asset("youtube", "v"), SPLIT, lang="en", auto_generated=True)
        assert len(cache.search_captions("the end")) == 1


def test_clear_after_old_index_rebuild_counts_dropped_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE caption_sources (provider TEXT, asset_id TEXT, lang TEXT, auto INTEGER, "
        "fetched_at REAL, ttl_s INTEGER, asset_json TEXT, PRIMARY KEY(provider, asset_id));"
        "CREATE VIRTUAL TABLE captions USING fts5(text, provider UNINDEXED, "
        "asset_id UNINDEXED, start_s UNINDEXED, end_s UNINDEXED);"
        "INSERT INTO caption_sources VALUES ('youtube', 'v', 'en', 1, 1e12, 999, '{}');"
        "INSERT INTO caption_sources VALUES ('youtube', 'w', 'en', 1, 1e12, 999, '{}');"
    )
    conn.commit()
    conn.close()
    with Cache(path) as cache:
        assert cache.clear_captions() == 2  # rows dropped by the rebuild are reported
        assert cache.clear_captions() == 0


def test_joined_era_index_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "joined.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE caption_sources (provider TEXT, asset_id TEXT, lang TEXT, auto INTEGER, "
        "fetched_at REAL, ttl_s INTEGER, asset_json TEXT, PRIMARY KEY(provider, asset_id));"
        "CREATE VIRTUAL TABLE captions USING fts5(text, provider UNINDEXED, "
        "asset_id UNINDEXED, start_s UNINDEXED, end_s UNINDEXED, joined UNINDEXED);"
        "INSERT INTO caption_sources VALUES ('youtube', 'v', 'en', 1, 1e12, 999, '{}');"
        "INSERT INTO captions VALUES ('stale text', 'youtube', 'v', 1.0, 2.0, 0);"
    )
    conn.commit()
    conn.close()
    with Cache(path) as cache:
        assert cache.has_captions("youtube", "v") is False  # refetched after the rebuild
        assert cache.search_captions("stale text") == []
        cache.put_captions(make_asset("youtube", "v"), FOUR, lang="en", auto_generated=True)
        rows = cache.search_captions("we never really knew")
        assert [(r.start_s, r.end_s, r.span) for r in rows] == [(2.0, 6.0, 3)]
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(captions)")}
    conn.close()
    assert "span" in columns and "joined" not in columns


@pytest.mark.parametrize(
    "query",
    ['say "hello', "hello*", "NEAR(hello", "a AND OR NOT", "col:val", "-x", "'", "^start", "(("],
)
def test_query_with_fts_syntax_does_not_raise(cache: Cache, query: str) -> None:
    cache.put_captions(make_asset("youtube", "v"), CUES, lang="en", auto_generated=True)
    assert isinstance(cache.search_captions(query), list)


def test_blank_query_returns_nothing(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "v"), CUES, lang="en", auto_generated=True)
    assert cache.search_captions('  " "  ') == []


def test_fts_phrase_quoting() -> None:
    assert fts_phrase("never gonna") == '"never gonna"'
    assert fts_phrase('say "hi"  now') == '"say hi now"'
    assert fts_phrase('""') == ""


def test_clear_captions(cache: Cache) -> None:
    cache.put_captions(make_asset("youtube", "a"), CUES, lang="en", auto_generated=True)
    cache.put_captions(make_asset("youtube", "b"), [], lang=None, auto_generated=False)
    assert cache.clear_captions() == 2
    assert cache.has_captions("youtube", "a") is False
    assert cache.search_captions("never") == []
    assert cache.clear_captions() == 0


def test_missing_fts5_raises_only_when_used(cache: Cache) -> None:
    class NoFts:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def executescript(self, script: str) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    cache._conn = NoFts(cache._conn)  # type: ignore[assignment]
    with pytest.raises(CaptionIndexUnavailableError, match="FTS5"):
        cache.has_captions("youtube", "v")
    assert issubclass(CaptionIndexUnavailableError, sqlite3.Error)
    cache._conn = cache._conn.conn  # type: ignore[attr-defined]
