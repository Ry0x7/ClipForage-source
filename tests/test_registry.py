from pathlib import Path

import pytest

from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers import registry
from clipforage.providers.base import Provider, ProviderError


def make_asset(provider: str, asset_id: str, **overrides: object) -> Asset:
    data: dict[str, object] = {
        "provider": provider,
        "id": asset_id,
        "kind": Kind.VIDEO,
        "title": f"asset {asset_id}",
        "source_url": f"https://example.com/{asset_id}",
        "license": "CC0-1.0",
        "attribution_text": "someone",
        "commercial_ok": True,
    }
    data.update(overrides)
    return Asset(**data)


class FakeProvider:
    def __init__(
        self,
        provider_id: str = "fake",
        kinds: frozenset[Kind] = frozenset({Kind.VIDEO}),
        assets: list[Asset] | None = None,
        requires_key: bool = False,
    ) -> None:
        self.id = provider_id
        self.kinds = kinds
        self.requires_key = requires_key
        self.assets = assets if assets is not None else [make_asset(provider_id, "1")]
        self.calls: list[tuple[str, Kind]] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.calls.append((query, kind))
        return list(self.assets)

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="http", url=asset.source_url, ext="mp4")


class BrokenProvider(FakeProvider):
    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        raise RuntimeError("boom")


class ErroringProvider(FakeProvider):
    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        raise ProviderError(self.id, "rate limited")


@pytest.fixture
def no_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLIPFORAGE_KEYED_KEY", raising=False)


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeProvider(), Provider)


def test_provider_error_str() -> None:
    err = ProviderError("pexels", "timeout")
    assert str(err) == "pexels: timeout"
    assert err.provider == "pexels"
    assert err.message == "timeout"


def test_register_get_and_duplicate() -> None:
    fake = FakeProvider()
    assert registry.register(fake) is fake
    assert registry.get("fake") is fake
    with pytest.raises(ValueError):
        registry.register(FakeProvider())


def test_get_unknown_raises_keyerror() -> None:
    registry.register(FakeProvider("alpha"))
    with pytest.raises(KeyError, match="nope.*alpha"):
        registry.get("nope")


def test_all_providers_and_providers_for_sorted() -> None:
    b = registry.register(FakeProvider("b", frozenset({Kind.VIDEO})))
    a = registry.register(FakeProvider("a", frozenset({Kind.VIDEO, Kind.GIF})))
    c = registry.register(FakeProvider("c", frozenset({Kind.GIF})))
    assert registry.all_providers() == [a, b, c]
    assert registry.providers_for(Kind.GIF) == [a, c]
    assert registry.providers_for(Kind.FONT) == []


def test_search_all_defaults_to_kind_providers() -> None:
    video = registry.register(FakeProvider("video", frozenset({Kind.VIDEO})))
    gif = registry.register(FakeProvider("gif", frozenset({Kind.GIF})))
    assets, errors = registry.search_all("cat", SearchFilters(kind=Kind.VIDEO))
    assert [a.provider for a in assets] == ["video"]
    assert errors == []
    assert video.calls == [("cat", Kind.VIDEO)]
    assert gif.calls == []


def test_failing_provider_is_wrapped_and_others_continue() -> None:
    registry.register(BrokenProvider("broken"))
    registry.register(FakeProvider("good"))
    assets, errors = registry.search_all("cat", SearchFilters(kind=Kind.VIDEO))
    assert [a.provider for a in assets] == ["good"]
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderError)
    assert errors[0].provider == "broken"
    assert str(errors[0]) == "broken: RuntimeError: boom"


def test_provider_error_passed_through() -> None:
    registry.register(ErroringProvider("limited"))
    registry.register(FakeProvider("good"))
    assets, errors = registry.search_all("cat", SearchFilters(kind=Kind.VIDEO))
    assert len(assets) == 1
    assert [str(e) for e in errors] == ["limited: rate limited"]


def test_keyed_provider_skipped_without_key(no_config: None) -> None:
    keyed = registry.register(FakeProvider("keyed", requires_key=True))
    assets, errors = registry.search_all("cat", SearchFilters(kind=Kind.VIDEO))
    assert assets == []
    assert keyed.calls == []
    assert len(errors) == 1
    assert errors[0].provider == "keyed"
    assert "CLIPFORAGE_KEYED_KEY" in errors[0].message


def test_keyed_provider_included_with_env(no_config: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_KEYED_KEY", "secret")
    keyed = registry.register(FakeProvider("keyed", requires_key=True))
    assets, errors = registry.search_all("cat", SearchFilters(kind=Kind.VIDEO))
    assert len(assets) == 1
    assert errors == []
    assert keyed.calls == [("cat", Kind.VIDEO)]


def test_explicit_providers_argument() -> None:
    registered = registry.register(FakeProvider("registered"))
    other = FakeProvider("other")
    assets, _ = registry.search_all("cat", SearchFilters(kind=Kind.VIDEO), providers=[other])
    assert [a.provider for a in assets] == ["other"]
    assert registered.calls == []


def test_client_side_filters() -> None:
    assets_in = [
        make_asset("p", "commercial", commercial_ok=True, duration_s=5.0, orientation="landscape"),
        make_asset("p", "noncommercial", commercial_ok=False),
        make_asset("p", "unknown", commercial_ok=None),
        make_asset("p", "short", duration_s=1.0),
        make_asset("p", "long", duration_s=100.0),
        make_asset("p", "noduration", duration_s=None),
        make_asset("p", "portrait", orientation="portrait"),
    ]
    registry.register(FakeProvider("p", assets=assets_in))

    def ids(filters: SearchFilters) -> list[str]:
        found, _ = registry.search_all("q", filters)
        return [a.id for a in found]

    assert ids(SearchFilters(kind=Kind.VIDEO)) == [a.id for a in assets_in]

    commercial = ids(SearchFilters(kind=Kind.VIDEO, commercial_only=True))
    assert "noncommercial" not in commercial
    assert "unknown" not in commercial
    assert "commercial" in commercial

    dur = ids(SearchFilters(kind=Kind.VIDEO, min_duration=2.0, max_duration=50.0))
    assert "short" not in dur
    assert "long" not in dur
    assert "commercial" in dur
    assert "noduration" in dur

    orient = ids(SearchFilters(kind=Kind.VIDEO, orientation="landscape"))
    assert "portrait" not in orient
    assert "commercial" in orient
    assert "noduration" in orient


SHORTS = SearchFilters(kind=Kind.VIDEO, shorts=True)


@pytest.mark.parametrize(
    ("duration_s", "kept"),
    [(30.0, True), (60.0, True), (60.5, False), (120.0, False), (None, False)],
)
def test_shorts_caps_video_at_60_s(duration_s: float | None, kept: bool) -> None:
    asset = make_asset("p", "v", duration_s=duration_s)
    assert registry._passes_filters(asset, SHORTS) is kept
    assert registry._passes_filters(asset, SearchFilters(kind=Kind.VIDEO)) is True


def test_shorts_leaves_non_video_kinds_untouched() -> None:
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, shorts=True)
    for duration_s in (30.0, 120.0, None):
        asset = make_asset("p", "a", kind=Kind.AUDIO_MUSIC, duration_s=duration_s)
        assert registry._passes_filters(asset, filters) is True


def test_shorts_cap_applies_to_every_provider() -> None:
    for provider_id in ("pexels", "wikimedia"):
        registry.register(
            FakeProvider(
                provider_id,
                assets=[
                    make_asset(provider_id, f"{provider_id}-short", duration_s=30.0),
                    make_asset(provider_id, f"{provider_id}-long", duration_s=120.0),
                    make_asset(provider_id, f"{provider_id}-unknown", duration_s=None),
                ],
            )
        )

    found, errors = registry.search_all("q", SHORTS)

    assert errors == []
    assert sorted(a.id for a in found) == ["pexels-short", "wikimedia-short"]


def test_malformed_config_is_isolated_per_provider(no_config: None) -> None:
    from clipforage.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[keys\nthis is = not toml", encoding="utf-8")
    registry.register(FakeProvider("keyed", requires_key=True))
    registry.register(FakeProvider("good"))
    assets, errors = registry.search_all("cat", SearchFilters(kind=Kind.VIDEO))
    assert [a.provider for a in assets] == ["good"]
    assert len(errors) == 1
    assert errors[0].provider == "keyed"
    assert "TOMLDecodeError" in errors[0].message


def test_search_all_uses_cache_across_searches(tmp_path: Path) -> None:
    from clipforage.cache import Cache

    fake = FakeProvider()
    filters = SearchFilters(kind=Kind.VIDEO)
    with Cache(tmp_path / "cache.db") as cache:
        first, errors1 = registry.search_all("cat", filters, providers=[fake], cache=cache)
        second, errors2 = registry.search_all("cat", filters, providers=[fake], cache=cache)
        assert len(fake.calls) == 1
        assert first == second == fake.assets
        assert errors1 == errors2 == []
        # use_cache=False bypasses the lookup but still stores
        registry.search_all("cat", filters, providers=[fake], cache=cache, use_cache=False)
        assert len(fake.calls) == 2


def test_search_all_honours_provider_ttl(tmp_path: Path) -> None:
    from clipforage.cache import Cache, make_key

    fake = FakeProvider()
    fake.SEARCH_TTL_S = 86400  # type: ignore[attr-defined]
    filters = SearchFilters(kind=Kind.VIDEO)
    with Cache(tmp_path / "cache.db") as cache:
        registry.search_all("cat", filters, providers=[fake], cache=cache)
        row = cache._conn.execute(
            "SELECT ttl_s FROM search_cache WHERE key = ?", (make_key("fake", "cat", filters),)
        ).fetchone()
    assert row == (86400,)


class FlakyProvider(FakeProvider):
    """Returns [] on the first search (a transient hiccup), then three assets."""

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.calls.append((query, kind))
        if len(self.calls) == 1:
            return []
        return [make_asset(self.id, str(i)) for i in range(3)]


def test_search_all_does_not_cache_empty_results(tmp_path: Path) -> None:
    from clipforage.cache import Cache

    flaky = FlakyProvider()
    filters = SearchFilters(kind=Kind.VIDEO)
    with Cache(tmp_path / "cache.db") as cache:
        first, _ = registry.search_all("undertale", filters, providers=[flaky], cache=cache)
        assert first == []
        assert cache.get_search("fake", "undertale", filters) is None  # nothing stored
        second, _ = registry.search_all("undertale", filters, providers=[flaky], cache=cache)
        assert len(flaky.calls) == 2  # not served from cache
        assert [a.id for a in second] == ["0", "1", "2"]
        third, _ = registry.search_all("undertale", filters, providers=[flaky], cache=cache)
        assert len(flaky.calls) == 2  # the non-empty result is cached as before
        assert third == second


def test_search_all_caches_empty_results_when_provider_opts_in(tmp_path: Path) -> None:
    from clipforage.cache import EMPTY_RESULT_TTL_S, Cache, make_key

    fake = FakeProvider(assets=[])
    fake.cache_empty_results = True  # type: ignore[attr-defined]
    fake.SEARCH_TTL_S = 86400  # type: ignore[attr-defined]
    filters = SearchFilters(kind=Kind.VIDEO)
    with Cache(tmp_path / "cache.db") as cache:
        first, errors = registry.search_all("nothing", filters, providers=[fake], cache=cache)
        assert first == [] and errors == []
        row = cache._conn.execute(
            "SELECT stored_at, ttl_s FROM search_cache WHERE key = ?",
            (make_key("fake", "nothing", filters),),
        ).fetchone()
        assert row is not None and row[1] == 86400  # stored with the provider's normal TTL
        second, _ = registry.search_all("nothing", filters, providers=[fake], cache=cache)
        assert second == []
        assert len(fake.calls) == 1  # served from cache
        # served past EMPTY_RESULT_TTL_S, up to the full TTL
        later = row[0] + EMPTY_RESULT_TTL_S + 1
        assert cache.get_search("fake", "nothing", filters, now=later, allow_empty=True) == []
        assert cache.get_search("fake", "nothing", filters, now=row[0] + 86401) is None


def test_stable_providers_cache_empty_results() -> None:
    from clipforage.providers import (
        freesound,
        google_fonts,
        internet_archive,
        lottiefiles,
        mixkit,
        openverse,
        pixabay,
        wikimedia,
        youtube,
    )

    opted_in = [
        pixabay.PixabayProvider,
        internet_archive.InternetArchiveProvider,
        google_fonts.GoogleFontsProvider,
        mixkit.MixkitProvider,
        lottiefiles.LottieFilesProvider,
    ]
    assert all(getattr(c, "cache_empty_results", False) is True for c in opted_in)
    for mod in (youtube, openverse, wikimedia, freesound):
        classes = [v for k, v in vars(mod).items() if k.endswith("Provider") and k != "Provider"]
        assert classes, mod
        assert not any(getattr(c, "cache_empty_results", False) for c in classes)


def test_old_empty_cache_entry_is_a_miss(tmp_path: Path) -> None:
    from clipforage.cache import EMPTY_RESULT_TTL_S, Cache

    filters = SearchFilters(kind=Kind.VIDEO)
    with Cache(tmp_path / "cache.db") as cache:
        # an empty entry written before empties stopped being cached
        cache.put_search("youtube", "undertale", filters, [], ttl_s=86400, now=1000.0)
        assert cache.get_search("youtube", "undertale", filters, now=1000.0 + 60) == []
        later = 1000.0 + EMPTY_RESULT_TTL_S + 1
        assert cache.get_search("youtube", "undertale", filters, now=later) is None
        count = cache._conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()
        assert count == (0,)  # the stale empty entry was deleted
        # non-empty entries keep their full TTL
        cache.put_search("youtube", "q", filters, [make_asset("youtube", "1")], 86400, now=1000.0)
        assert cache.get_search("youtube", "q", filters, now=later) is not None


def test_search_all_cache_failure_falls_through(tmp_path: Path) -> None:
    import sqlite3

    class BrokenCache:
        def get_search(self, *args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        def put_search(self, *args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    fake = FakeProvider()
    assets, errors = registry.search_all(
        "cat",
        SearchFilters(kind=Kind.VIDEO),
        providers=[fake],
        cache=BrokenCache(),  # type: ignore[arg-type]
    )
    assert assets == fake.assets
    assert len(fake.calls) == 1
    assert [str(e) for e in errors] == [
        "fake: cache: database is locked",
        "fake: cache: disk I/O error",
    ]


def test_load_builtins_registers_pexels_and_pixabay() -> None:
    registry.load_builtins()
    registry.load_builtins()  # idempotent
    assert {"youtube", "pexels", "pixabay"} <= {p.id for p in registry.all_providers()}


def test_load_builtins_registers_m2_providers_idempotently() -> None:
    registry.load_builtins()
    first = {p.id: p for p in registry.all_providers()}
    registry.load_builtins()
    assert {p.id: p for p in registry.all_providers()} == first
    assert {"openverse", "wikimedia", "freesound", "internet_archive", "google_fonts"} <= set(first)


def test_load_builtins_skips_already_registered_m2_provider() -> None:
    fake = FakeProvider("openverse")
    registry.register(fake)
    registry.load_builtins()
    assert registry.get("openverse") is fake


def test_providers_for_font_and_sfx_after_builtins() -> None:
    registry.load_builtins()
    fonts = {p.id for p in registry.providers_for(Kind.FONT)}
    sfx = {p.id for p in registry.providers_for(Kind.AUDIO_SFX)}
    assert "google_fonts" in fonts
    assert {"freesound", "openverse", "wikimedia", "internet_archive"} <= sfx


def test_builtin_keyed_providers_skipped_without_key(
    no_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIPFORAGE_PEXELS_KEY", raising=False)
    monkeypatch.delenv("CLIPFORAGE_PIXABAY_KEY", raising=False)
    registry.load_builtins()
    keyed = [registry.get("pexels"), registry.get("pixabay")]
    assets, errors = registry.search_all("rain", SearchFilters(kind=Kind.VIDEO), providers=keyed)
    assert assets == []
    assert sorted(e.provider for e in errors) == ["pexels", "pixabay"]
    assert "CLIPFORAGE_PEXELS_KEY" in next(e.message for e in errors if e.provider == "pexels")


class FakeEntryPoint:
    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self.target = target
        self.loads = 0

    def load(self) -> object:
        self.loads += 1
        if isinstance(self.target, BaseException):
            raise self.target
        return self.target


@pytest.fixture
def fake_plugins(monkeypatch: pytest.MonkeyPatch) -> list[FakeEntryPoint]:
    eps = [
        FakeEntryPoint("inst", FakeProvider("plug_inst")),
        FakeEntryPoint("factory", lambda: FakeProvider("plug_factory")),
        FakeEntryPoint("broken", ImportError("No module named 'nope'")),
        FakeEntryPoint("notprov", object()),
        FakeEntryPoint("dup", FakeProvider("pexels")),
        FakeEntryPoint("klass", FakeProvider),
    ]

    def fake_entry_points(*, group: str) -> list[FakeEntryPoint]:
        return eps if group == "clipforage.providers" else []

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)
    return eps


def test_load_plugins_isolates_failures(fake_plugins: list[FakeEntryPoint]) -> None:
    registry.register(FakeProvider("pexels"))
    warnings: list[str] = []
    ids = registry.load_plugins(warn=warnings.append)
    assert ids == ["plug_inst", "plug_factory", "fake"]
    assert registry.get("plug_factory").id == "plug_factory"
    assert isinstance(registry.get("fake"), FakeProvider)  # class entry point instantiated
    assert warnings == [
        "plugin broken: ImportError: No module named 'nope'",
        "plugin notprov: TypeError: object is not a Provider",
        "plugin dup: ValueError: duplicate provider id 'pexels'",
    ]


def test_load_plugins_factory_exception_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_factory() -> Provider:
        raise RuntimeError("no config")

    eps = [FakeEntryPoint("bad", bad_factory), FakeEntryPoint("ok", FakeProvider("ok"))]
    monkeypatch.setattr("importlib.metadata.entry_points", lambda *, group: eps)
    warnings: list[str] = []
    assert registry.load_plugins(warn=warnings.append) == ["ok"]
    assert warnings == ["plugin bad: RuntimeError: no config"]


def test_load_plugins_defaults_to_logger(
    fake_plugins: list[FakeEntryPoint], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="clipforage"):
        registry.load_plugins()
    assert "plugin broken: ImportError" in caplog.text


def test_load_builtins_loads_plugins_after_builtins_idempotently(
    fake_plugins: list[FakeEntryPoint], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="clipforage"):
        registry.load_builtins()
        registry.load_builtins()
    ids = {p.id for p in registry.all_providers()}
    assert {"plug_inst", "plug_factory", "fake", "pexels", "youtube"} <= ids
    assert type(registry.get("pexels")).__name__ == "PexelsProvider"  # built-in wins
    assert caplog.text.count("plugin dup: ValueError: duplicate provider id 'pexels'") == 1
    assert all(ep.loads == 1 for ep in fake_plugins)
    assert registry.load_plugins() == []


def test_load_plugins_also_loads_legacy_group(monkeypatch: pytest.MonkeyPatch) -> None:
    groups = {
        "clipforage.providers": [FakeEntryPoint("new", FakeProvider("plug_new"))],
        "memefinder.providers": [
            FakeEntryPoint("old", FakeProvider("plug_old")),
            FakeEntryPoint("old_broken", ImportError("gone")),
            FakeEntryPoint("new", FakeProvider("plug_new_again")),  # same name: loaded once
        ],
    }
    monkeypatch.setattr("importlib.metadata.entry_points", lambda *, group: groups.get(group, []))
    warnings: list[str] = []
    assert registry.LEGACY_PLUGIN_GROUP == "memefinder.providers"
    assert registry.load_plugins(warn=warnings.append) == ["plug_new", "plug_old"]
    assert warnings == ["plugin old_broken: ImportError: gone"]
    assert registry.load_plugins() == []
