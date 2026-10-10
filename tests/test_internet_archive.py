import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.models import Asset, Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.internet_archive import (
    CLEARED_CLAUSE,
    SEARCH_URL,
    InternetArchiveProvider,
    license_from_url,
    safe_clause,
)

FIXTURES = Path(__file__).parent / "fixtures" / "internet_archive"
VIDEO_FILTERS = SearchFilters(kind=Kind.VIDEO)
AUDIO_FILTERS = SearchFilters(kind=Kind.AUDIO_SFX)
EXPECTED_FIELDS = [
    "identifier",
    "title",
    "mediatype",
    "licenseurl",
    "creator",
    "description",
    "date",
    "downloads",
    "subject",
    "collection",
]


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client()


@pytest.fixture
def provider(client: httpx.Client) -> InternetArchiveProvider:
    return InternetArchiveProvider(client=client)


def test_satisfies_provider_protocol(provider: InternetArchiveProvider) -> None:
    assert isinstance(provider, Provider)
    assert provider.id == "internet_archive"
    assert provider.requires_key is False
    assert provider.SEARCH_TTL_S == 86400
    assert provider.kinds == {Kind.VIDEO, Kind.AUDIO_MUSIC, Kind.AUDIO_SFX, Kind.IMAGE}


@respx.mock
def test_video_search_request_and_mapping(provider: InternetArchiveProvider) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_movies.json"))
    )
    assets = provider.search("cat", Kind.VIDEO, VIDEO_FILTERS)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["q"] == "(cat) AND mediatype:(movies)" + safe_clause()
    assert params.get_list("fl[]") == EXPECTED_FIELDS
    assert params["rows"] == "20"
    assert params["page"] == "1"
    assert params["sort[]"] == "downloads desc"
    assert params["output"] == "json"

    assert [a.id for a in assets] == ["cat_green_screen_2012", "old_cartoon_cat"]
    licensed, unlicensed = assets
    for asset in assets:
        assert asset.provider == "internet_archive"
        assert asset.kind == Kind.VIDEO
        assert asset.source_url == f"https://archive.org/details/{asset.id}"
        assert asset.preview_url == f"https://archive.org/services/img/{asset.id}"
        assert asset.duration_s is None
        assert asset.orientation is None
    assert licensed.title == "Cat Green Screen"
    assert licensed.license == "CC BY 3.0"
    assert licensed.commercial_ok is True
    assert licensed.attribution_text == (
        "Cat Green Screen by Jane Doe, Internet Archive (cat_green_screen_2012)"
    )
    assert licensed.extra == {
        "identifier": "cat_green_screen_2012",
        "mediatype": "movies",
        "license_url": "http://creativecommons.org/licenses/by/3.0/",
        "subject": [],
        "collection": [],
        "description": "A cat on a green screen.",
    }
    assert unlicensed.license == "unknown"
    assert unlicensed.commercial_ok is None
    assert unlicensed.attribution_text == (
        "Old Cartoon Cat by unknown, Internet Archive (old_cartoon_cat)"
    )
    assert unlicensed.extra["license_url"] is None


@pytest.mark.parametrize(
    ("kind", "mediatype"),
    [
        (Kind.VIDEO, "movies"),
        (Kind.AUDIO_MUSIC, "audio"),
        (Kind.AUDIO_SFX, "audio"),
        (Kind.IMAGE, "image"),
    ],
)
@respx.mock
def test_query_mediatype_clause_per_kind(
    provider: InternetArchiveProvider, kind: Kind, mediatype: str
) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"response": {"docs": []}})
    )
    assert provider.search("rain", kind, SearchFilters(kind=kind, safe=False)) == []
    assert route.calls.last.request.url.params["q"] == f"(rain) AND mediatype:({mediatype})"


@respx.mock
def test_audio_search_cc0_and_list_creator(provider: InternetArchiveProvider) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_audio.json"))
    )
    (asset,) = provider.search("whoosh", Kind.AUDIO_SFX, AUDIO_FILTERS)
    assert asset.kind == Kind.AUDIO_SFX
    assert asset.license == "CC0 1.0"
    assert asset.commercial_ok is True
    assert asset.attribution_text == (
        "Whoosh SFX Pack by Sound Person, Helper, Internet Archive (whoosh_sfx_pack)"
    )


@pytest.mark.parametrize(
    ("url", "label", "commercial"),
    [
        ("http://creativecommons.org/publicdomain/zero/1.0/", "CC0 1.0", True),
        ("https://creativecommons.org/publicdomain/mark/1.0/", "Public Domain Mark", True),
        ("http://creativecommons.org/licenses/by/4.0/", "CC BY 4.0", True),
        ("http://creativecommons.org/licenses/by-sa/3.0/", "CC BY-SA 3.0", True),
        ("http://creativecommons.org/licenses/by-nc/3.0/", "CC BY-NC 3.0", False),
        ("http://creativecommons.org/licenses/by-nc-sa/4.0/", "CC BY-NC-SA 4.0", False),
        ("http://creativecommons.org/licenses/by-nc-nd/3.0/us/", "CC BY-NC-ND 3.0", False),
        ("http://creativecommons.org/licenses/by-nd/2.5/", "CC BY-ND 2.5", True),
        (None, "unknown", None),
        ("", "unknown", None),
        ("https://example.com/some-license", "unknown", None),
    ],
)
def test_license_mapping(url: str | None, label: str, commercial: bool | None) -> None:
    assert license_from_url(url) == (label, commercial)


@respx.mock
def test_unsupported_kind_returns_empty_without_http(provider: InternetArchiveProvider) -> None:
    route = respx.get(url__startswith="https://archive.org/")
    assert provider.search("q", Kind.FONT, SearchFilters(kind=Kind.FONT)) == []
    assert route.call_count == 0


@pytest.mark.parametrize("status", [500, 503])
@respx.mock
def test_server_error_raises_provider_error(provider: InternetArchiveProvider, status: int) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(status, text="backend down"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert info.value.provider == "internet_archive"
    assert str(status) in info.value.message


@respx.mock
def test_429_includes_retry_after(provider: InternetArchiveProvider) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, text="slow down")
    )
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert "429" in info.value.message
    assert "Retry-After: 30" in info.value.message


@respx.mock
def test_bad_json_and_transport_error_raise_provider_error(
    provider: InternetArchiveProvider,
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ProviderError):
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert "ConnectError" in info.value.message


def _search_one(provider: InternetArchiveProvider, fixture: str, kind: Kind) -> Asset:
    # Callers are decorated with @respx.mock, so routes added here share that router.
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=load_fixture(fixture)))
    return provider.search("q", kind, SearchFilters(kind=kind))[0]


@respx.mock
def test_resolve_video_picks_derivative_mp4_quoted(provider: InternetArchiveProvider) -> None:
    asset = _search_one(provider, "search_movies.json", Kind.VIDEO)
    route = respx.get("https://archive.org/metadata/cat_green_screen_2012").mock(
        return_value=httpx.Response(200, json=load_fixture("metadata_video.json"))
    )
    plan = provider.resolve(asset)
    assert route.call_count == 1
    assert plan.method == "http"
    assert plan.url == (
        "https://archive.org/download/cat_green_screen_2012/Cat%20Green%20Screen.mp4"
    )
    assert plan.ext == "mp4"
    assert plan.asset.duration_s == 12.34
    assert plan.asset.id == asset.id
    assert asset.duration_s is None  # original asset untouched


@respx.mock
def test_resolve_audio_picks_vbr_mp3(provider: InternetArchiveProvider) -> None:
    asset = _search_one(provider, "search_audio.json", Kind.AUDIO_SFX)
    respx.get("https://archive.org/metadata/whoosh_sfx_pack").mock(
        return_value=httpx.Response(200, json=load_fixture("metadata_audio.json"))
    )
    plan = provider.resolve(asset)
    assert plan.url == "https://archive.org/download/whoosh_sfx_pack/whoosh.mp3"
    assert plan.ext == "mp3"
    assert plan.asset.duration_s == 3.20


def _audio_meta(files: list[dict[str, str]]) -> dict[str, Any]:
    return {"metadata": {"identifier": "whoosh_sfx_pack"}, "files": files}


@respx.mock
def test_resolve_audio_accepts_derivative_mp3_without_vbr(
    provider: InternetArchiveProvider,
) -> None:
    asset = _search_one(provider, "search_audio.json", Kind.AUDIO_SFX)
    files = [
        {"name": "whoosh.flac", "source": "original", "format": "Flac", "size": "2400000"},
        {
            "name": "whoosh_64kb.mp3",
            "source": "derivative",
            "format": "64Kbps MP3",
            "size": "30000",
        },
        {"name": "whoosh.mp3", "source": "derivative", "format": "128Kbps MP3", "size": "60000"},
    ]
    respx.get("https://archive.org/metadata/whoosh_sfx_pack").mock(
        return_value=httpx.Response(200, json=_audio_meta(files))
    )
    plan = provider.resolve(asset)
    assert plan.url == "https://archive.org/download/whoosh_sfx_pack/whoosh.mp3"
    assert plan.ext == "mp3"


@respx.mock
def test_resolve_audio_prefers_original_mp3_over_vbr(provider: InternetArchiveProvider) -> None:
    asset = _search_one(provider, "search_audio.json", Kind.AUDIO_SFX)
    files = [
        {"name": "take.mp3", "source": "original", "format": "MP3", "size": "90000"},
        {"name": "take_vbr.mp3", "source": "derivative", "format": "VBR MP3", "size": "120000"},
        {"name": "take_128.mp3", "source": "derivative", "format": "128Kbps MP3", "size": "200000"},
    ]
    respx.get("https://archive.org/metadata/whoosh_sfx_pack").mock(
        return_value=httpx.Response(200, json=_audio_meta(files))
    )
    assert provider.resolve(asset).url.endswith("/take.mp3")


@respx.mock
def test_resolve_audio_falls_back_to_original_non_mp3(provider: InternetArchiveProvider) -> None:
    asset = _search_one(provider, "search_audio.json", Kind.AUDIO_SFX)
    files = [{"name": "whoosh.wav", "source": "original", "format": "WAVE", "size": "900000"}]
    respx.get("https://archive.org/metadata/whoosh_sfx_pack").mock(
        return_value=httpx.Response(200, json=_audio_meta(files))
    )
    plan = provider.resolve(asset)
    assert plan.url.endswith("/whoosh.wav")
    assert plan.ext == "wav"


@respx.mock
def test_resolve_without_suitable_file_raises(provider: InternetArchiveProvider) -> None:
    asset = _search_one(provider, "search_audio.json", Kind.AUDIO_SFX)
    video_meta = load_fixture("metadata_video.json")
    respx.get("https://archive.org/metadata/whoosh_sfx_pack").mock(
        return_value=httpx.Response(200, json=video_meta)
    )
    with pytest.raises(ProviderError) as info:
        provider.resolve(asset)
    assert info.value.provider == "internet_archive"
    assert "no suitable" in info.value.message


@respx.mock
def test_resolve_http_error_raises_provider_error(provider: InternetArchiveProvider) -> None:
    asset = _search_one(provider, "search_audio.json", Kind.AUDIO_SFX)
    respx.get("https://archive.org/metadata/whoosh_sfx_pack").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    with pytest.raises(ProviderError) as info:
        provider.resolve(asset)
    assert "502" in info.value.message


@respx.mock
def test_safe_query_adds_not_clauses_only_when_safe(provider: InternetArchiveProvider) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"response": {"docs": []}})
    )
    provider.search("rain", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    safe_q = route.calls.last.request.url.params["q"]
    assert safe_q.startswith("(rain) AND mediatype:(movies) AND NOT collection:(")
    assert '"adultvhs"' in safe_q and '"no-preview"' in safe_q
    assert " AND NOT subject:(porn OR " in safe_q
    provider.search("rain", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO, safe=False))
    assert route.calls.last.request.url.params["q"] == "(rain) AND mediatype:(movies)"


@respx.mock
def test_subject_and_collection_kept_without_fav_lists(provider: InternetArchiveProvider) -> None:
    doc = {
        "identifier": "vhs_tape",
        "title": "Home Tape",
        "mediatype": "movies",
        "subject": ["vhs", "home movie"],
        "collection": ["home_movies", "fav-someuser_naughty_list", "moviesandfilms"],
        "description": "x" * 900,
    }
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"response": {"docs": [doc]}}))
    (asset,) = provider.search("tape", Kind.VIDEO, VIDEO_FILTERS)
    assert asset.extra["subject"] == ["vhs", "home movie"]
    assert asset.extra["collection"] == ["home_movies", "moviesandfilms"]
    assert asset.extra["description"] == "x" * 500


@respx.mock
@pytest.mark.parametrize("kind", [Kind.AUDIO_MUSIC, Kind.AUDIO_SFX])
def test_cleared_only_audio_query_requires_open_license(
    provider: InternetArchiveProvider, kind: Kind
) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_audio.json"))
    )
    (asset,) = provider.search("lofi", kind, SearchFilters(kind=kind, cleared_only=True))
    params = route.calls.last.request.url.params
    assert CLEARED_CLAUSE == " AND licenseurl:* AND NOT licenseurl:*nc*"
    assert params["q"] == "(lofi) AND mediatype:(audio)" + CLEARED_CLAUSE + safe_clause()
    assert "licenseurl" in params.get_list("fl[]")
    assert asset.license == "CC0 1.0"


@respx.mock
def test_cleared_only_does_not_touch_video_or_plain_audio_query(
    provider: InternetArchiveProvider,
) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"response": {"docs": []}})
    )
    provider.search("cat", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO, cleared_only=True))
    assert "licenseurl" not in route.calls.last.request.url.params["q"]
    provider.search("lofi", Kind.AUDIO_MUSIC, SearchFilters(kind=Kind.AUDIO_MUSIC))
    assert "licenseurl" not in route.calls.last.request.url.params["q"]
