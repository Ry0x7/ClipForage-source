"""Safe search: blocklist matching, the registry post-filter and the setting."""

from pathlib import Path

import pytest

from clipforage import config, safesearch
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers import registry


def make_asset(asset_id: str = "1", **overrides: object) -> Asset:
    data: dict[str, object] = {
        "provider": "fake",
        "id": asset_id,
        "kind": Kind.VIDEO,
        "title": f"asset {asset_id}",
        "source_url": f"https://example.com/{asset_id}",
        "license": "CC0-1.0",
        "attribution_text": "someone",
        "commercial_ok": True,
    }
    data.update(overrides)
    return Asset(**data)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "title",
    [
        "Hot XXX compilation",
        "PORN green screen",
        "nsfw meme",
        "Nude beach day",
        "naked and afraid",
        "p0rn obfuscated",
        "best_hentai_clips",
        "erotic-film-1970",
        "Adult film night",
        "x-rated cartoon",
        "R-18 MMD",
        "sex tape leaked",
    ],
)
def test_explicit_titles_blocked(title: str) -> None:
    assert safesearch.is_explicit(make_asset(title=title))


@pytest.mark.parametrize(
    "title",
    [
        "How to assess risk",
        "Class of 1999",
        "Cockatoo dancing",
        "Essex countryside",
        "Sussex coast drone",
        "Scunthorpe United highlights",
        "Middlesex hospital",
        "Adult education lecture",
        "Cock-a-doodle-doo rooster sound",
        "Moby Dick audiobook",
        "Blue tit at the feeder",
        "Summa cum laude graduation",
        "Hardcore techno mix",
        "Pussywillow branches",
        "Analysis of variance",
        "Sextant navigation",
        "Therapist explains",
        "Penthouse view timelapse",
        "Green screen crying cat",
        "Sex Pistols - Anarchy in the UK",  # allow phrase
        "Naked Ladies (Amaryllis belladonna) in bloom",  # allow phrase
        "Naked mole-rat colony",  # allow phrase
    ],
)
def test_innocent_titles_not_blocked(title: str) -> None:
    assert not safesearch.is_explicit(make_asset(title=title))


@pytest.mark.parametrize(
    "extra",
    [
        {"tags": ["cat", "nsfw"]},
        {"subject": "Pornography; history"},
        {"description": "Uncensored hentai episode"},
        {"categories": ["Nude women", "Photographs"]},
        {"collection": ["adultvhs", "moviesandfilms"]},
        {"collection": "no-preview"},
        {"collection": ["FROSTEDPORNFLAKES"]},  # identifier match is case-insensitive
    ],
)
def test_extra_fields_blocked(extra: dict[str, object]) -> None:
    assert safesearch.is_explicit(make_asset(extra=extra))


@pytest.mark.parametrize(
    "extra",
    [
        {"tags": ["cat", "funny"]},
        {"subject": ["adult education", "sexuality studies"]},
        {"collection": ["moviesandfilms", "prelinger"]},
        {"categories": ["Featured pictures of cats"]},
        {"tags": 42},  # non-string values are ignored
        {"width": "nsfw"},  # only the listed tag-like keys are read
    ],
)
def test_extra_fields_innocent(extra: dict[str, object]) -> None:
    assert not safesearch.is_explicit(make_asset(extra=extra))


def test_url_path_matched_but_not_host_or_query() -> None:
    assert safesearch.is_explicit(
        make_asset(source_url="https://www.pexels.com/video/porn-runner-123/")
    )
    assert safesearch.is_explicit(
        make_asset(source_url="https://commons.wikimedia.org/wiki/File:Hentai_study_%281890%29.jpg")
    )
    assert not safesearch.is_explicit(make_asset(source_url="https://sex.example.com/cat?q=nsfw"))
    assert not safesearch.is_explicit(make_asset(source_url="https://archive.org/details/essex"))


def test_url_path_matches_strong_terms_only() -> None:
    assert not safesearch.is_explicit(
        make_asset(source_url="https://www.pexels.com/video/naked-runner-123/")
    )
    assert not safesearch.is_explicit(make_asset(source_url="https://archive.org/details/JailBait"))


def test_jail_bait_1954_kept() -> None:
    asset = make_asset(
        provider="internet_archive",
        title="Jail Bait (1954)",
        source_url="https://archive.org/details/JailBait",
        extra={
            "description": "Ed Wood crime drama, often sold as a sexploitation picture.",
            "subject": ["film noir", "public domain"],
            "collection": ["feature_films", "moviesandfilms"],
        },
    )
    assert not safesearch.is_explicit(asset)


def test_naked_ladies_flower_kept() -> None:
    asset = make_asset(
        provider="wikimedia",
        title="Naked ladies (Amaryllis belladonna)",
        source_url="https://commons.wikimedia.org/wiki/File:Naked_ladies_flower.jpg",
        extra={"categories": ["Amaryllis belladonna", "Naked ladies"]},
    )
    assert not safesearch.is_explicit(asset)


def test_mild_term_in_description_alone_kept() -> None:
    asset = make_asset(
        title="Lighthouse at dusk",
        extra={"description": "Shot from a nude beach near the cliffs; sex of the gulls unknown."},
    )
    assert not safesearch.is_explicit(asset)


def test_strong_term_in_description_blocks() -> None:
    asset = make_asset(title="Lighthouse at dusk", extra={"description": "full porn version"})
    assert safesearch.is_explicit(asset)


@pytest.mark.parametrize("key", ["tags", "subject", "collection", "categories"])
def test_mild_term_in_tag_like_fields_blocks(key: str) -> None:
    assert safesearch.is_explicit(make_asset(extra={key: ["landscape", "nude"]}))


def test_mild_term_in_title_blocks() -> None:
    assert safesearch.is_explicit(make_asset(title="Fetish photo shoot"))
    assert safesearch.is_explicit(make_asset(title="Vulva close-up"))
    assert safesearch.text_is_explicit("naked runner")
    assert not safesearch.text_is_explicit("naked runner", mild=False)


@pytest.mark.parametrize(
    "text",
    [
        "naked ladies hentai",
        "naked ladies and naked men",
        "sex pistols sex tape",
        "nude mouse porn",
    ],
)
def test_allow_phrase_does_not_unblock_other_terms(text: str) -> None:
    assert safesearch.is_explicit(make_asset(title=text))


def test_allow_phrase_cut_does_not_join_neighbours() -> None:
    # "adult" + "film" around a cut phrase must not become the strong phrase "adult film".
    assert not safesearch.text_is_explicit("adult naked eye film")
    assert safesearch.text_is_explicit("adult film")


def test_filter_explicit_counts() -> None:
    clean = [make_asset("1"), make_asset("3", title="rain on window")]
    dirty = [make_asset("2", title="xxx"), make_asset("4", extra={"tags": ["porn"]})]
    kept, dropped = safesearch.filter_explicit([clean[0], dirty[0], clean[1], dirty[1]])
    assert kept == clean
    assert dropped == 2
    assert safesearch.filter_explicit([]) == ([], 0)


def test_blocklist_table_shape() -> None:
    words, phrases = safesearch._terms("terms")
    mild_words, mild_phrases = safesearch._terms("mild_terms")
    total = len(words) + len(phrases) + len(mild_words) + len(mild_phrases)
    assert 60 <= total <= 200
    assert 10 <= len(mild_words) + len(mild_phrases) < len(words) + len(phrases)
    for ws, ps in ((words, phrases), (mild_words, mild_phrases)):
        assert all(w == safesearch.normalize(w) and " " not in w for w in ws)
        assert all(" " in p for p in ps)
    strong = words | set(phrases)
    mild = mild_words | set(mild_phrases)
    allow = [safesearch.normalize(p) for p in safesearch._table()["allow_phrases"]]
    assert 1 <= len(allow) <= 30
    assert all(" " in p for p in allow)
    assert len(set(allow)) == len(allow)
    assert strong.isdisjoint(mild)
    assert strong.isdisjoint(allow) and mild.isdisjoint(allow)
    for ambiguous in ("adult", "cock", "dick", "tit", "ass", "cum", "hardcore"):
        assert ambiguous not in strong | mild
    assert "adultvhs" in safesearch.ia_collections()
    assert "porn" in safesearch.ia_subjects()


def test_configured_safe_default_env_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    assert safesearch.configured_safe() is True
    config.set_setting("search", "safe", False)
    assert safesearch.configured_safe() is False
    monkeypatch.setenv("CLIPFORAGE_SAFE_SEARCH", "on")
    assert safesearch.configured_safe() is True
    monkeypatch.setenv("CLIPFORAGE_SAFE_SEARCH", "0")
    config.set_setting("search", "safe", True)
    assert safesearch.configured_safe() is False


def test_resolve_safe_explicit_wins() -> None:
    config.set_setting("search", "safe", False)
    assert safesearch.resolve_safe(None) is False
    assert safesearch.resolve_safe(True) is True
    assert safesearch.resolve_safe(False) is False


class FakeProvider:
    id = "fake"
    kinds = frozenset({Kind.VIDEO})
    requires_key = False

    def __init__(self, assets: list[Asset]) -> None:
        self.assets = assets

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        return list(self.assets)

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="http", url=asset.source_url, ext="mp4")


def test_registry_drops_explicit_when_safe() -> None:
    fake = FakeProvider([make_asset("1"), make_asset("2", title="XXX video")])
    dropped: list[Asset] = []
    per_provider, errors = registry.search_each(
        "q", SearchFilters(kind=Kind.VIDEO), [fake], dropped=dropped
    )
    assert errors == []
    assert [a.id for a in per_provider[0]] == ["1"]
    assert [a.id for a in dropped] == ["2"]


def test_registry_keeps_explicit_when_unsafe() -> None:
    fake = FakeProvider([make_asset("1"), make_asset("2", title="XXX video")])
    dropped: list[Asset] = []
    assets, _ = registry.search_all(
        "q", SearchFilters(kind=Kind.VIDEO, safe=False), [fake], dropped=dropped
    )
    assert [a.id for a in assets] == ["1", "2"]
    assert dropped == []


def test_registry_filters_cached_results_too(tmp_path: Path) -> None:
    from clipforage.cache import Cache

    fake = FakeProvider([make_asset("1"), make_asset("2", title="nsfw")])
    filters = SearchFilters(kind=Kind.VIDEO)
    with Cache(tmp_path / "cache.db") as cache:
        registry.search_all("q", filters, [fake], cache=cache)
        fake.assets = []  # second call must come from the cache
        dropped: list[Asset] = []
        assets, _ = registry.search_all("q", filters, [fake], cache=cache, dropped=dropped)
    assert [a.id for a in assets] == ["1"]
    assert [a.id for a in dropped] == ["2"]


def test_search_expanded_filtered_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforage import service

    monkeypatch.setattr(registry, "load_builtins", lambda: None)
    registry.register(FakeProvider([make_asset("1"), make_asset("2", title="porn")]))
    filters = SearchFilters(kind=Kind.VIDEO, expand=False, related=False)
    outcome = service.search_expanded("q", filters, use_cache=False)
    assert [a.id for a in outcome.assets] == ["1"]
    assert outcome.filtered_count == 1
    unsafe = filters.model_copy(update={"safe": False})
    outcome = service.search_expanded("q", unsafe, use_cache=False)
    assert [a.id for a in outcome.assets] == ["1", "2"]
    assert outcome.filtered_count == 0
