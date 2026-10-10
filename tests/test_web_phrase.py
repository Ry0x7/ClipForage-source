"""GET /api/phrase (web phrase search). service.phrase_search is faked; no network."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clipforage import service
from clipforage.models import Asset, Kind, PhraseHit, PhraseOutcome
from clipforage.providers.base import ProviderError
from clipforage.web import app as web_app


def make_hit(
    provider: str, asset_id: str, start: float, end: float, text: str, auto: bool = False
) -> PhraseHit:
    url = (
        f"https://www.youtube.com/watch?v={asset_id}"
        if provider == "youtube"
        else f"https://archive.org/details/{asset_id}"
    )
    asset = Asset(
        provider=provider,
        id=asset_id,
        kind=Kind.VIDEO,
        title=f"Title {asset_id}",
        source_url=url,
        license="unknown",
        attribution_text="attr",
        commercial_ok=None,
    )
    return PhraseHit(
        asset=asset, start_s=start, end_s=end, text=text, lang="en", auto_generated=auto
    )


class FakePhraseSearch:
    def __init__(
        self, hits: list[PhraseHit], errors: list[ProviderError], filtered_count: int = 0
    ) -> None:
        self.hits = hits
        self.errors = errors
        self.filtered_count = filtered_count
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, query: str, **kwargs: Any) -> PhraseOutcome:
        self.calls.append((query, kwargs))
        return PhraseOutcome(self.hits, self.errors, self.filtered_count)


HITS = [
    make_hit("youtube", "dQw4w9WgXcQ", 43.2, 45.9, "never gonna give you up", auto=True),
    make_hit("internet_archive", "his_girl_friday", 176.2, 178.4, "didn't sign that reprieve"),
]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePhraseSearch:
    found = FakePhraseSearch(HITS, [ProviderError("internet_archive", "boom")], filtered_count=2)
    monkeypatch.setattr(service, "phrase_search", found)
    return found


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(web_app.create_app(), base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def test_phrase_response_shape(client: TestClient, fake: FakePhraseSearch) -> None:
    response = client.get("/api/phrase", params={"q": "never gonna"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [h["ref"] for h in body["hits"]] == [
        "youtube:dQw4w9WgXcQ",
        "internet_archive:his_girl_friday",
    ]
    for out, hit in zip(body["hits"], HITS, strict=True):
        assert out["clip"] == hit.clip_spec()
        assert out["start_s"] == hit.start_s
        assert out["end_s"] == hit.end_s
        assert out["text"] == hit.text
        assert out["lang"] == "en"
        assert out["auto_generated"] == hit.auto_generated
        assert out["asset"]["ref"] == hit.ref
        assert out["asset"]["title"] == hit.asset.title
        assert out["asset"]["license"] == "unknown"
    assert body["hits"][1]["clip"] == "175.2-179.4"
    assert body["errors"] == [{"provider": "internet_archive", "message": "boom"}]
    assert [link["name"] for link in body["linkouts"]] == ["PlayPhrase"]
    assert body["filtered_count"] == 2


def test_phrase_passes_parsed_kwargs(client: TestClient, fake: FakePhraseSearch) -> None:
    response = client.get(
        "/api/phrase",
        params=[
            ("q", "  the governor  "),
            ("source", "youtube"),
            ("source", "internet_archive"),
            ("within", " his_girl_friday "),
            ("max_videos", "3"),
            ("limit", "7"),
            ("max_per_video", "0"),
            ("no_cache", "true"),
        ],
    )
    assert response.status_code == 200
    assert fake.calls == [
        (
            "the governor",
            {
                "sources": ["youtube", "internet_archive"],
                "within": "his_girl_friday",
                "max_videos": 3,
                "limit": 7,
                "use_cache": False,
                "max_per_video": 0,
                "safe": None,
            },
        )
    ]


def test_phrase_safe_false_reaches_service(client: TestClient, fake: FakePhraseSearch) -> None:
    assert client.get("/api/phrase", params={"q": "x", "safe": "false"}).status_code == 200
    assert fake.calls[0][1]["safe"] is False


def test_phrase_defaults(client: TestClient, fake: FakePhraseSearch) -> None:
    assert client.get("/api/phrase", params={"q": "x", "within": ""}).status_code == 200
    assert fake.calls == [
        (
            "x",
            {
                "sources": None,
                "within": None,
                "max_videos": 10,
                "limit": 20,
                "use_cache": True,
                "max_per_video": 3,
                "safe": None,
            },
        )
    ]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({}, 422),
        ({"q": ""}, 422),
        ({"q": "   "}, 422),
        ({"q": "x", "source": "pexels"}, 404),
        ({"q": "x", "max_videos": "0"}, 422),
        ({"q": "x", "max_videos": "26"}, 422),
        ({"q": "x", "limit": "0"}, 422),
        ({"q": "x", "limit": "101"}, 422),
        ({"q": "x", "max_per_video": "-1"}, 422),
        ({"q": "x", "max_per_video": "51"}, 422),
    ],
)
def test_phrase_validation(
    client: TestClient, fake: FakePhraseSearch, params: dict[str, str], status: int
) -> None:
    assert client.get("/api/phrase", params=params).status_code == status
    assert fake.calls == []


def test_phrase_cache_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(query: str, **kwargs: Any) -> Any:
        raise service.CacheUnavailableError("cache unavailable: no fts5")

    monkeypatch.setattr(service, "phrase_search", broken)
    response = client.get("/api/phrase", params={"q": "x"})
    assert response.status_code == 503
    assert "no fts5" in response.json()["detail"]
