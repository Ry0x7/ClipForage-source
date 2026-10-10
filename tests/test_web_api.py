"""Local web API (FastAPI TestClient): search, preview, download jobs, settings. No live network."""

import json
import time
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from clipforage import config
from clipforage.cache import Cache
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers import internet_archive, pexels, pixabay, registry
from clipforage.web import app as web_app

FIXTURES = Path(__file__).parent / "fixtures"
HEADERS = {"X-Clipforage": "1"}


def load(provider: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / provider / name).read_text(encoding="utf-8"))


class FakeProvider:
    """Stands in for youtube / wikimedia / internet_archive (no network)."""

    kinds = frozenset({Kind.VIDEO})
    requires_key = False

    def __init__(self, provider_id: str) -> None:
        self.id = provider_id

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        return []

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="http", url=asset.source_url, ext="mp4")


@pytest.fixture(autouse=True)
def _keys_and_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_PEXELS_KEY", "test-pexels-key")
    monkeypatch.setenv("CLIPFORAGE_PIXABAY_KEY", "test-pixabay-key")
    for provider_id in ("youtube", "wikimedia", "internet_archive", "mixkit"):
        registry.register(FakeProvider(provider_id))
    registry.load_builtins()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(web_app.create_app(), base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def mock_apis() -> None:
    respx.get(pexels.VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load("pexels", "videos_search.json"))
    )
    respx.get(pixabay.VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load("pixabay", "videos_search.json"))
    )


def run_search(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/search", params={"q": "rain window", "kind": "video"})
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_job(client: TestClient, job_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def test_index_and_static(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<h1>ClipForage</h1>" in response.text
    assert response.headers["cache-control"] == "no-cache"  # revalidate: no stale UI
    static = client.get("/static/app.js")
    assert static.status_code == 200
    assert static.headers["cache-control"] == "no-cache"


def test_providers_list(client: TestClient) -> None:
    response = client.get("/api/providers")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    by_id = {p["id"]: p for p in response.json()}
    assert {"pexels", "pixabay", "youtube", "wikimedia"} <= set(by_id)
    assert by_id["pexels"]["requires_key"] is True
    assert by_id["pexels"]["has_key"] is True


@respx.mock
def test_search_returns_assets_with_ref_and_linkouts(client: TestClient) -> None:
    mock_apis()
    body = run_search(client)
    refs = {a["ref"] for a in body["assets"]}
    assert "pexels:3571264" in refs
    assert "pixabay:125" in refs
    for asset in body["assets"]:
        assert asset["ref"] == f"{asset['provider']}:{asset['id']}"
        assert asset["license"]
    assert body["errors"] == []
    assert any(link["name"] == "Videvo" for link in body["linkouts"])


class RecordingProvider(FakeProvider):
    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id)
        self.filters: list[SearchFilters] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.filters.append(filters)
        return []


def test_search_style_reaches_provider(client: TestClient) -> None:
    fake = RecordingProvider("recorder")  # youtube is already a FakeProvider here
    registry.register(fake)
    params = {"q": "crying cat", "kind": "video", "provider": "recorder", "style": "meme"}
    response = client.get("/api/search", params={**params, "limit": "8", "no_cache": "true"})
    assert response.status_code == 200, response.text
    assert fake.filters[-1].style == "meme"
    assert fake.filters[-1].limit == 8
    del params["style"]
    assert client.get("/api/search", params={**params, "no_cache": "true"}).status_code == 200
    assert fake.filters[-1].style == "any"


def test_search_shorts_reaches_provider(client: TestClient) -> None:
    fake = RecordingProvider("recorder")  # youtube is already a FakeProvider here
    registry.register(fake)
    params = {"q": "crying cat", "kind": "video", "provider": "recorder", "shorts": "true"}
    response = client.get("/api/search", params={**params, "no_cache": "true"})
    assert response.status_code == 200, response.text
    assert fake.filters[-1].shorts is True
    del params["shorts"]
    assert client.get("/api/search", params={**params, "no_cache": "true"}).status_code == 200
    assert fake.filters[-1].shorts is False


class FixedProvider(FakeProvider):
    """Returns ``count`` assets for any query; kind GIF so no other test provider competes."""

    kinds = frozenset({Kind.GIF})

    def __init__(self, provider_id: str, count: int) -> None:
        super().__init__(provider_id)
        self.count = count

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        return [
            Asset(
                provider=self.id,
                id=str(i),
                kind=kind,
                title=f"{self.id} {i}",
                source_url=f"https://example.com/{self.id}/{i}",
                license="CC0-1.0",
                attribution_text="CC0",
                commercial_ok=True,
            )
            for i in range(self.count)
        ]


@respx.mock
def test_search_limit_interleaves_providers(client: TestClient) -> None:
    registry.register(FixedProvider("aa_first", 5))
    registry.register(FixedProvider("ab_second", 5))
    params = {"q": "cat", "kind": "gif", "limit": "2", "no_cache": "true"}
    response = client.get("/api/search", params=params)
    assert response.status_code == 200, response.text
    refs = [a["ref"] for a in response.json()["assets"]]
    assert refs == ["aa_first:0", "ab_second:0"]


def test_search_invalid_style_is_422(client: TestClient) -> None:
    response = client.get("/api/search", params={"q": "x", "style": "vlog"})
    assert response.status_code == 422
    assert "invalid style" in response.text


def test_search_invalid_kind_is_422(client: TestClient) -> None:
    response = client.get("/api/search", params={"q": "x", "kind": "nope"})
    assert response.status_code == 422
    assert "invalid kind 'nope'" in response.json()["detail"]


def test_search_invalid_orientation_is_422(client: TestClient) -> None:
    response = client.get("/api/search", params={"q": "x", "orientation": "diagonal"})
    assert response.status_code == 422
    assert "invalid orientation" in response.json()["detail"]


def test_search_unknown_provider_is_404(client: TestClient) -> None:
    response = client.get("/api/search", params={"q": "x", "provider": "nope"})
    assert response.status_code == 404


def test_preview_unknown_ref_is_404(client: TestClient) -> None:
    assert client.get("/api/preview/pexels/nonexistent").status_code == 404


@respx.mock
def test_preview_proxies_cached_asset(client: TestClient) -> None:
    mock_apis()
    run_search(client)
    with Cache() as cache:
        asset = cache.find_asset("pexels", "3571264")
    assert asset is not None and asset.preview_url
    route = respx.get(asset.preview_url).mock(
        return_value=httpx.Response(
            200, content=b"jpegbytes", headers={"content-type": "image/jpeg"}
        )
    )
    response = client.get("/api/preview/pexels/3571264")
    assert response.status_code == 200
    assert response.content == b"jpegbytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert route.call_count == 1


def test_host_header_must_be_loopback(client: TestClient) -> None:
    response = client.get("/api/providers", headers={"Host": "evil.example"})
    assert response.status_code == 400
    response = client.get("/api/providers", headers={"Host": "127.0.0.1:8765"})
    assert response.status_code == 200
    assert client.get("/api/providers", headers={"Host": "localhost:8765"}).status_code == 200
    assert client.get("/api/providers", headers={"Host": "[::1]:8765"}).status_code == 200


def test_extra_allowed_host() -> None:
    with TestClient(web_app.create_app(["192.168.1.5"])) as test_client:
        response = test_client.get("/api/providers", headers={"Host": "192.168.1.5:8765"})
        assert response.status_code == 200
        response = test_client.get("/api/providers", headers={"Host": "evil.example"})
        assert response.status_code == 400


def test_create_app_migrates_legacy_config_dir(capsys: pytest.CaptureFixture[str]) -> None:
    old = config._config_dir("memefinder")
    old.mkdir(parents=True)
    (old / "config.toml").write_text('[keys]\npixabay = "x"\n', encoding="utf-8")
    web_app.create_app()
    assert "renamed to clipforage" in capsys.readouterr().err
    assert not old.exists()
    assert config.load_config()["keys"]["pixabay"] == "x"


@respx.mock
def test_preview_redirect_to_other_host_is_502(client: TestClient) -> None:
    mock_apis()
    run_search(client)
    with Cache() as cache:
        asset = cache.find_asset("pexels", "3571264")
    assert asset is not None and asset.preview_url
    respx.get(asset.preview_url).mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/x.jpg"})
    )
    other = respx.get("https://evil.example/x.jpg").mock(
        return_value=httpx.Response(200, content=b"evil")
    )
    response = client.get("/api/preview/pexels/3571264")
    assert response.status_code == 502
    assert "another host" in response.json()["detail"]
    assert b"evil" not in response.content
    assert other.call_count <= 1


MEDIA_URL = "https://cdn.freesound.example/previews/1/whoosh-hq.mp3"


def seed_asset(**overrides: Any) -> Asset:
    """Put one asset straight into the search cache so /api/media can find it."""
    fields: dict[str, Any] = {
        "provider": "pexels",
        "id": "777",
        "kind": Kind.AUDIO_SFX,
        "title": "Whoosh",
        "source_url": "https://example.org/whoosh",
        "license": "CC0-1.0",
        "attribution_text": "Whoosh by someone (CC0)",
        "commercial_ok": True,
        **overrides,
    }
    asset = Asset(**fields)
    with Cache() as cache:
        cache.put_search(
            asset.provider, "seed", SearchFilters(kind=asset.kind), [asset], ttl_s=3600
        )
    return asset


@respx.mock
def test_media_streams_media_url(client: TestClient) -> None:
    seed_asset(media_url=MEDIA_URL, media_type="audio/mpeg")
    route = respx.get(MEDIA_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"ID3mp3bytes",
            headers={"content-type": "audio/mpeg", "accept-ranges": "bytes"},
        )
    )
    response = client.get("/api/media/pexels/777")
    assert response.status_code == 200
    assert response.content == b"ID3mp3bytes"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["content-length"] == str(len(b"ID3mp3bytes"))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert route.call_count == 1
    assert "range" not in route.calls.last.request.headers


@respx.mock
def test_media_forwards_range_and_returns_206(client: TestClient) -> None:
    seed_asset(media_url=MEDIA_URL)
    route = respx.get(MEDIA_URL).mock(
        return_value=httpx.Response(
            206,
            content=b"0123",
            headers={
                "content-type": "audio/mpeg",
                "content-range": "bytes 0-3/100",
                "accept-ranges": "bytes",
            },
        )
    )
    response = client.get("/api/media/pexels/777", headers={"Range": "bytes=0-3"})
    assert route.calls.last.request.headers["range"] == "bytes=0-3"
    assert response.status_code == 206
    assert response.content == b"0123"
    assert response.headers["content-range"] == "bytes 0-3/100"
    assert response.headers["content-length"] == "4"
    assert response.headers["accept-ranges"] == "bytes"


def test_media_without_media_url_is_404(client: TestClient) -> None:
    seed_asset()
    response = client.get("/api/media/pexels/777")
    assert response.status_code == 404
    assert "no media" in response.json()["detail"]


def test_media_unknown_ref_is_404(client: TestClient) -> None:
    assert client.get("/api/media/pexels/nonexistent").status_code == 404


@respx.mock
def test_media_redirect_to_other_host_is_502(client: TestClient) -> None:
    seed_asset(media_url=MEDIA_URL)
    respx.get(MEDIA_URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/x.mp3"})
    )
    respx.get("https://evil.example/x.mp3").mock(return_value=httpx.Response(200, content=b"evil"))
    response = client.get("/api/media/pexels/777")
    assert response.status_code == 502
    assert "another host" in response.json()["detail"]


@respx.mock
def test_media_redirect_to_same_site_subdomain_streams(client: TestClient) -> None:
    origin = "https://archive.org/download/whoosh_sfx_pack/whoosh.mp3"
    mirror = "https://dn721805.ca.archive.org/0/items/whoosh_sfx_pack/whoosh.mp3"
    seed_asset(media_url=origin)
    respx.get(origin).mock(return_value=httpx.Response(302, headers={"location": mirror}))
    respx.get(mirror).mock(
        return_value=httpx.Response(200, content=b"iamp3", headers={"content-type": "audio/mpeg"})
    )
    response = client.get("/api/media/pexels/777")
    assert response.status_code == 200
    assert response.content == b"iamp3"
    assert response.headers["content-type"] == "audio/mpeg"


@respx.mock
def test_media_redirect_to_lookalike_site_is_502(client: TestClient) -> None:
    origin = "https://archive.org/download/whoosh_sfx_pack/whoosh.mp3"
    seed_asset(media_url=origin)
    respx.get(origin).mock(
        return_value=httpx.Response(302, headers={"location": "https://archive.org.evil.com/x"})
    )
    respx.get("https://archive.org.evil.com/x").mock(
        return_value=httpx.Response(200, content=b"evil")
    )
    response = client.get("/api/media/pexels/777")
    assert response.status_code == 502
    assert b"evil" not in response.content


@respx.mock
def test_media_passes_through_upstream_416(client: TestClient) -> None:
    seed_asset(media_url=MEDIA_URL)
    respx.get(MEDIA_URL).mock(return_value=httpx.Response(416, content=b"range not satisfiable"))
    response = client.get("/api/media/pexels/777", headers={"Range": "bytes=999-"})
    assert response.status_code == 416
    assert response.content == b""


@respx.mock
def test_media_passes_through_upstream_404(client: TestClient) -> None:
    seed_asset(media_url=MEDIA_URL)
    respx.get(MEDIA_URL).mock(return_value=httpx.Response(404, content=b"gone"))
    response = client.get("/api/media/pexels/777")
    assert response.status_code == 404
    assert response.content == b""


@respx.mock
def test_media_upstream_5xx_is_502(client: TestClient) -> None:
    seed_asset(media_url=MEDIA_URL)
    respx.get(MEDIA_URL).mock(return_value=httpx.Response(503))
    response = client.get("/api/media/pexels/777")
    assert response.status_code == 502


@pytest.mark.parametrize(
    ("host_a", "host_b", "expected"),
    [
        ("archive.org", "archive.org", True),
        ("archive.org", "dn721805.ca.archive.org", True),
        ("pexels.com", "videos.pexels.com", True),
        ("Archive.ORG", "archive.org.", True),
        ("evil.com", "archive.org", False),
        ("archive.org.evil.com", "archive.org", False),
        ("localhost", "archive.org", False),
        ("a.co.uk", "b.co.uk", False),
        ("bbc.co.uk", "www.bbc.co.uk", True),
        ("co.uk", "x.co.uk", False),
        ("cdn.pixabay.com", "pixabay.com", True),
        ("example.com.au", "other.com.au", False),
    ],
)
def test_same_site(host_a: str, host_b: str, expected: bool) -> None:
    assert web_app._same_site(host_a, host_b) is expected


@respx.mock
def test_media_internet_archive_falls_back_to_resolve(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        registry._PROVIDERS, "internet_archive", internet_archive.InternetArchiveProvider()
    )
    seed_asset(
        provider="internet_archive",
        id="whoosh_sfx_pack",
        extra={"identifier": "whoosh_sfx_pack"},
    )
    meta = respx.get(internet_archive.METADATA_URL.format(identifier="whoosh_sfx_pack")).mock(
        return_value=httpx.Response(200, json=load("internet_archive", "metadata_audio.json"))
    )
    file_url = internet_archive.DOWNLOAD_URL.format(
        identifier="whoosh_sfx_pack", filename="whoosh.mp3"
    )
    stream = respx.get(file_url).mock(
        return_value=httpx.Response(200, content=b"iamp3", headers={"content-type": "audio/mpeg"})
    )
    response = client.get("/api/media/internet_archive/whoosh_sfx_pack")
    assert response.status_code == 200, response.text
    assert response.content == b"iamp3"
    assert response.headers["content-type"] == "audio/mpeg"
    assert meta.call_count == 1
    assert stream.call_count == 1


def test_post_without_header_is_403(client: TestClient) -> None:
    response = client.post("/api/download", json={"ref": "pexels:1"})
    assert response.status_code == 403
    response = client.put("/api/settings/keys/pexels", json={"key": "abc"})
    assert response.status_code == 403


@pytest.mark.parametrize("header", ["X-Clipforage", "X-Memefinder"])
def test_mutating_request_with_new_or_legacy_header_passes_guard(
    client: TestClient, header: str
) -> None:
    response = client.put("/api/settings/keys/pexels", json={"key": "abc"}, headers={header: "1"})
    assert response.status_code == 200
    assert config.load_config()["keys"]["pexels"] == "abc"


def test_wrong_header_value_is_403(client: TestClient) -> None:
    response = client.put(
        "/api/settings/keys/pexels", json={"key": "abc"}, headers={"X-Clipforage": "0"}
    )
    assert response.status_code == 403


@respx.mock
def test_download_job_for_cached_ref_completes(client: TestClient, tmp_path: Path) -> None:
    mock_apis()
    run_search(client)
    with Cache() as cache:
        asset = cache.find_asset("pexels", "3571264")
    assert asset is not None
    media = respx.get(asset.extra["download_url"]).mock(
        return_value=httpx.Response(200, content=b"fake mp4 bytes")
    )
    library = tmp_path / "library"
    config.set_setting("library", "dir", str(library))

    response = client.post("/api/download", json={"ref": "pexels:3571264"}, headers=HEADERS)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert job["progress"] == 1.0
    assert media.call_count == 1
    path = Path(job["path"])
    assert path.parent == library
    assert path.read_bytes() == b"fake mp4 bytes"
    assert Path(job["sidecar"]).is_file()
    assert job["license"] == "Pexels License"
    assert "Joey Farina" in job["attribution_text"]
    listed = client.get("/api/jobs").json()
    assert listed[0]["job_id"] == job["job_id"]
    history = client.get("/api/history").json()
    assert history[0]["provider"] == "pexels"
    assert history[0]["asset_id"] == "3571264"
    assert job["history_id"] == history[0]["id"]
    served = client.get(f"/api/library/{job['history_id']}/file")
    assert served.status_code == 200
    assert served.content == b"fake mp4 bytes"


def download_pexels_result(client: TestClient, tmp_path: Path) -> dict[str, Any]:
    """Search, then download pexels:3571264 into a tmp library; return the finished job."""
    mock_apis()
    run_search(client)
    with Cache() as cache:
        asset = cache.find_asset("pexels", "3571264")
    assert asset is not None
    respx.get(asset.extra["download_url"]).mock(
        return_value=httpx.Response(200, content=b"fake mp4 bytes")
    )
    config.set_setting("library", "dir", str(tmp_path / "library"))
    response = client.post("/api/download", json={"ref": "pexels:3571264"}, headers=HEADERS)
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])
    assert job["status"] == "done", job
    return job


def search_asset(client: TestClient, ref: str) -> dict[str, Any]:
    return next(a for a in run_search(client)["assets"] if a["ref"] == ref)


@respx.mock
def test_search_marks_downloaded_asset_with_history_id(client: TestClient, tmp_path: Path) -> None:
    job = download_pexels_result(client, tmp_path)
    found = search_asset(client, "pexels:3571264")
    assert found["history_id"] == job["history_id"]
    assert Path(found["history_path"]) == Path(job["path"]).resolve()
    others = [a for a in run_search(client)["assets"] if a["ref"] != "pexels:3571264"]
    assert others and all(a["history_id"] is None for a in others)
    served = client.get(f"/api/library/{found['history_id']}/file")
    assert served.status_code == 200
    assert served.content == b"fake mp4 bytes"
    with Cache() as cache:  # computed per response, never stored in the search cache
        payloads = [r[0] for r in cache._conn.execute("SELECT payload FROM search_cache")]
    assert payloads and not any("history_id" in p for p in payloads)


@respx.mock
def test_search_history_id_is_null_when_file_deleted(client: TestClient, tmp_path: Path) -> None:
    job = download_pexels_result(client, tmp_path)
    Path(job["path"]).unlink()
    found = search_asset(client, "pexels:3571264")
    assert found["history_id"] is None
    assert found["history_path"] is None


def test_download_bad_ref_is_400_with_hint(client: TestClient) -> None:
    response = client.post("/api/download", json={"ref": "pexels:nonexistent"}, headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["hint"] == "run a search first so the asset is cached"


def test_download_needs_exactly_one_of_ref_or_url(client: TestClient) -> None:
    response = client.post("/api/download", json={}, headers=HEADERS)
    assert response.status_code == 400
    response = client.post(
        "/api/download", json={"ref": "pexels:1", "url": "https://x.test/"}, headers=HEADERS
    )
    assert response.status_code == 400


@respx.mock
def test_download_mp3_for_non_ytdlp_ref_is_400(client: TestClient) -> None:
    mock_apis()
    run_search(client)
    response = client.post(
        "/api/download", json={"ref": "pexels:3571264", "mp3": True}, headers=HEADERS
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "mp3 only applies to yt-dlp sources"
    assert client.get("/api/jobs").json() == []


class DirectAudioProvider(FakeProvider):
    """A Freesound-like source: audio assets served as a direct file of a fixed ext."""

    kinds = frozenset({Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})

    def __init__(self, provider_id: str, ext: str) -> None:
        super().__init__(provider_id)
        self.ext = ext

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="http", url=asset.source_url, ext=self.ext)


@respx.mock
def test_download_mp3_for_direct_mp3_audio_ref_is_accepted(
    client: TestClient, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    config.set_setting("library", "dir", str(library))
    registry.register(DirectAudioProvider("audiodirect", "mp3"))
    seed_asset(provider="audiodirect", source_url="https://cdn.example.org/whoosh.mp3")
    media = respx.get("https://cdn.example.org/whoosh.mp3").mock(
        return_value=httpx.Response(200, content=b"mp3 bytes")
    )

    response = client.post(
        "/api/download", json={"ref": "audiodirect:777", "mp3": True}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert media.call_count == 1
    assert Path(job["path"]).suffix == ".mp3"
    assert Path(job["path"]).read_bytes() == b"mp3 bytes"


@respx.mock
def test_download_mp3_for_direct_non_mp3_audio_ref_fails_the_job(
    client: TestClient, tmp_path: Path
) -> None:
    config.set_setting("library", "dir", str(tmp_path / "library"))
    registry.register(DirectAudioProvider("audiodirect", "ogg"))
    seed_asset(provider="audiodirect", source_url="https://cdn.example.org/whoosh.ogg")
    media = respx.get("https://cdn.example.org/whoosh.ogg").mock(
        return_value=httpx.Response(200, content=b"ogg bytes")
    )

    response = client.post(
        "/api/download", json={"ref": "audiodirect:777", "mp3": True}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "error", job
    assert "--mp3 only applies to yt-dlp sources" in job["error"]
    assert media.call_count == 0
    assert not (tmp_path / "library").exists()


def test_download_clip_for_http_video_ref_runs_ffmpeg_clip(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage.download import service as dl
    from clipforage.download.sidecar import write_sidecar

    library = tmp_path / "library"
    config.set_setting("library", "dir", str(library))
    seed_asset(
        provider="internet_archive",
        id="cat_film",
        kind=Kind.VIDEO,
        title="Cat film",
        source_url="https://archive.org/details/cat_film",
    )
    calls: list[tuple[str, tuple[float, float]]] = []

    def fake_clip(
        plan: DownloadPlan, clip: tuple[float, float], out_dir: Path | None = None, **kw: Any
    ) -> Any:
        calls.append((plan.url, clip))
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "Cat film [cat_film] 2-4.mp4"
        path.write_bytes(b"clip")
        fetched_at = datetime.now(UTC)
        extra = {"format": "mp4", "clip": {"start_s": clip[0], "end_s": clip[1]}}
        sidecar = write_sidecar(path, plan.asset, fetched_at, extra)
        return dl.DownloadResult(path=path, sidecar=sidecar, fetched_at=fetched_at)

    monkeypatch.setattr(dl, "download_http_clip", fake_clip)
    response = client.post(
        "/api/download", json={"ref": "internet_archive:cat_film", "clip": "2-4"}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert calls == [("https://archive.org/details/cat_film", (2.0, 4.0))]
    sidecar = client.get(f"/api/library/{job['history_id']}/sidecar")
    assert sidecar.status_code == 200
    assert sidecar.json()["clip"] == {"start_s": 2.0, "end_s": 4.0}


def test_download_clip_max_wall_s_reaches_ffmpeg_clip(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage.download import service as dl
    from clipforage.download.sidecar import write_sidecar

    config.set_setting("library", "dir", str(tmp_path / "library"))
    seed_asset(provider="internet_archive", id="cat_film", kind=Kind.VIDEO, title="Cat film")
    seen: list[dict[str, Any]] = []

    def fake_clip(
        plan: DownloadPlan, clip: tuple[float, float], out_dir: Path | None = None, **kw: Any
    ) -> Any:
        seen.append(kw)
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "Cat film [cat_film] 2-4.mp4"
        path.write_bytes(b"clip")
        fetched_at = datetime.now(UTC)
        sidecar = write_sidecar(path, plan.asset, fetched_at, {"format": "mp4"})
        return dl.DownloadResult(path=path, sidecar=sidecar, fetched_at=fetched_at)

    monkeypatch.setattr(dl, "download_http_clip", fake_clip)
    body = {"ref": "internet_archive:cat_film", "clip": "2-4", "max_wall_s": 42}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert seen[0]["max_wall_s"] == 42


def test_download_negative_max_wall_s_is_422(client: TestClient) -> None:
    body = {"ref": "internet_archive:cat_film", "clip": "2-4", "max_wall_s": -1}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 422


def test_download_max_wall_s_above_cap_is_400(client: TestClient) -> None:
    body = {"ref": "internet_archive:cat_film", "clip": "2-4", "max_wall_s": 99999}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"] == "max_wall_s above 7200"


@pytest.mark.parametrize("kind", [Kind.IMAGE, Kind.GIF, Kind.FONT])
def test_download_clip_for_non_media_ref_is_400(client: TestClient, kind: Kind) -> None:
    seed_asset(provider="wikimedia", id="pic", kind=kind, title="Picture")
    response = client.post(
        "/api/download", json={"ref": "wikimedia:pic", "clip": "0-5"}, headers=HEADERS
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "clip only applies to video/audio sources"
    assert client.get("/api/jobs").json() == []


def test_download_bad_clip_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/download", json={"url": "https://x.test/v", "clip": "10-5"}, headers=HEADERS
    )
    assert response.status_code == 400
    assert "invalid clip" in response.json()["detail"]


def test_settings_masks_keys(client: TestClient) -> None:
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["keys"]["pexels"] == "test…-key"
    assert "test-pexels-key" not in response.text
    assert body["config_path"] == str(config.config_path())
    assert body["library_dir"] == str(config.library_dir())
    assert any(p["id"] == "pexels" for p in body["providers"])


def test_put_key_persists_masked_and_empty_removes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIPFORAGE_FREESOUND_KEY", raising=False)
    response = client.put(
        "/api/settings/keys/freesound", json={"key": "freesound-secret-1234"}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json() == {"provider": "freesound", "key": "free…1234"}
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert data["keys"]["freesound"] == "freesound-secret-1234"
    settings = client.get("/api/settings")
    assert settings.json()["keys"]["freesound"] == "free…1234"
    assert "freesound-secret-1234" not in settings.text

    response = client.put("/api/settings/keys/freesound", json={"key": ""}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["key"] is None
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert "freesound" not in data["keys"]
    assert client.get("/api/settings").json()["keys"]["freesound"] is None


def test_put_key_unknown_provider_is_404(client: TestClient) -> None:
    response = client.put("/api/settings/keys/nope", json={"key": "x"}, headers=HEADERS)
    assert response.status_code == 404


def test_put_library_dir(client: TestClient, tmp_path: Path) -> None:
    response = client.put(
        "/api/settings/library_dir", json={"dir": "relative/lib"}, headers=HEADERS
    )
    assert response.status_code == 400
    target = tmp_path / "lib"
    response = client.put("/api/settings/library_dir", json={"dir": str(target)}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["library_dir"] == str(target)
    assert config.library_dir() == target


def test_doctor_returns_keys(client: TestClient) -> None:
    response = client.get("/api/doctor")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ffmpeg", "deno", "yt_dlp", "ejs"}
    assert set(body["ffmpeg"]) == {"found", "path"}
    assert body["yt_dlp"]["version"]
    assert isinstance(body["ejs"], bool)


def test_open_outside_library_is_403_inside_opens(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(web_app, "open_in_file_manager", opened.append)
    library = tmp_path / "library"
    (library / "sub").mkdir(parents=True)
    config.set_setting("library", "dir", str(library))

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    response = client.post("/api/open", json={"path": str(outside)}, headers=HEADERS)
    assert response.status_code == 403
    sneaky = library / ".." / "elsewhere"
    response = client.post("/api/open", json={"path": str(sneaky)}, headers=HEADERS)
    assert response.status_code == 403
    assert opened == []

    response = client.post("/api/open", json={"path": str(library / "sub")}, headers=HEADERS)
    assert response.status_code == 200
    assert opened == [(library / "sub").resolve()]


def record_library_file(
    tmp_path: Path, name: str = "rain clip.mp4", body: bytes = b"0123456789"
) -> tuple[int, Path]:
    """Write a file + sidecar into a temp library, record it in history, return (id, path)."""
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    config.set_setting("library", "dir", str(library))
    path = library / name
    path.write_bytes(body)
    sidecar = library / f"{name}.json"
    sidecar.write_text(json.dumps({"license": "CC0-1.0"}), encoding="utf-8")
    asset = Asset(
        provider="wikimedia",
        id="1",
        kind=Kind.VIDEO,
        title="rain",
        source_url="https://example.com/rain",
        license="CC0-1.0",
        attribution_text="",
        commercial_ok=True,
    )
    with Cache() as cache:
        history_id = cache.record_download(
            url="https://example.com/rain",
            path=path,
            sidecar=sidecar,
            asset=asset,
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    return history_id, path


def test_library_file_serves_bytes_with_type_and_disposition(
    client: TestClient, tmp_path: Path
) -> None:
    history_id, _ = record_library_file(tmp_path)
    response = client.get(f"/api/library/{history_id}/file")
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"] == 'inline; filename="rain clip.mp4"'
    assert response.headers["accept-ranges"] == "bytes"


def test_library_file_range_request_is_206(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path)
    response = client.get(f"/api/library/{history_id}/file", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"


def test_library_file_non_ascii_name_uses_rfc5987(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path, name="lluvia ñ.mp3")
    response = client.get(f"/api/library/{history_id}/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["content-disposition"] == (
        "inline; filename=\"lluvia _.mp3\"; filename*=UTF-8''lluvia%20%C3%B1.mp3"
    )


def test_library_file_unknown_id_or_missing_file_is_404(client: TestClient, tmp_path: Path) -> None:
    history_id, path = record_library_file(tmp_path)
    assert client.get(f"/api/library/{history_id + 999}/file").status_code == 404
    path.unlink()
    assert client.get(f"/api/library/{history_id}/file").status_code == 404


def test_library_file_outside_library_is_403(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config.set_setting("library", "dir", str(elsewhere))
    assert client.get(f"/api/library/{history_id}/file").status_code == 403
    assert client.get(f"/api/library/{history_id}/sidecar").status_code == 403


def test_library_sidecar_serves_json(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path)
    response = client.get(f"/api/library/{history_id}/sidecar")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"license": "CC0-1.0"}
    assert response.headers["content-disposition"] == 'inline; filename="rain clip.mp4.json"'


def _fake_download_result(tmp_path: Path) -> Any:
    from clipforage.download.service import asset_from_url
    from clipforage.download.sidecar import write_sidecar
    from clipforage.models import DownloadResult

    path = tmp_path / "Big Buck Bunny [abc123].mp4"
    path.write_bytes(b"mp4")
    fetched_at = datetime.now(UTC)
    asset = asset_from_url("https://www.youtube.com/watch?v=abc123", {"id": "abc123"})
    sidecar = write_sidecar(path, asset, fetched_at, {"format": "mp4"})
    return DownloadResult(path=path, sidecar=sidecar, fetched_at=fetched_at)


def test_download_url_quality_reaches_download_url(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage import service

    seen: list[dict[str, Any]] = []

    def fake_url(url: str, out: Path | None = None, **kw: Any) -> Any:
        seen.append(kw)
        return _fake_download_result(tmp_path)

    monkeypatch.setattr(service, "download_url", fake_url)
    body = {"url": "https://www.youtube.com/watch?v=abc123", "quality": "720"}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert seen[0]["quality"] == "720"


def test_download_job_shows_postprocessing_stage(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from clipforage import service

    release = threading.Event()

    def fake_url(url: str, out: Path | None = None, **kw: Any) -> Any:
        hook = kw["progress"]
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        hook({"status": "postprocessing", "postprocessor": "Merger", "fraction": None})
        assert release.wait(5.0)
        result = _fake_download_result(tmp_path)
        hook({"status": "finished", "filename": str(result.path), "fraction": 1.0})
        return result

    monkeypatch.setattr(service, "download_url", fake_url)
    body = {"url": "https://www.youtube.com/watch?v=abc123"}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    deadline = time.monotonic() + 5.0
    mid = client.get(f"/api/jobs/{job_id}").json()
    while mid.get("stage") != "postprocessing" and time.monotonic() < deadline:
        time.sleep(0.02)
        mid = client.get(f"/api/jobs/{job_id}").json()
    release.set()
    assert mid["stage"] == "postprocessing", mid
    assert mid["progress"] == 0.5  # last known fraction is kept, not reset
    job = wait_for_job(client, job_id)

    assert job["status"] == "done", job
    assert job["stage"] is None
    assert job["progress"] == 1.0


def test_download_ref_quality_reaches_download_ref(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage import service

    seed_asset(provider="youtube", id="abc123", kind=Kind.VIDEO, title="Big Buck Bunny")
    seen: list[dict[str, Any]] = []

    def fake_ref(ref: str, **kw: Any) -> Any:
        seen.append(kw)
        return "https://www.youtube.com/watch?v=abc123", _fake_download_result(tmp_path)

    monkeypatch.setattr(service, "download_ref", fake_ref)
    body = {"ref": "youtube:abc123", "quality": "1080"}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert seen[0]["quality"] == "1080"


@pytest.mark.parametrize("mode", ["ref", "url"])
def test_download_quality_best_is_forwarded(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from clipforage import service

    seen: list[dict[str, Any]] = []
    url = "https://www.youtube.com/watch?v=abc123"
    if mode == "ref":
        seed_asset(provider="youtube", id="abc123", kind=Kind.VIDEO, title="Big Buck Bunny")

        def fake_ref(ref: str, **kw: Any) -> Any:
            seen.append(kw)
            return url, _fake_download_result(tmp_path)

        monkeypatch.setattr(service, "download_ref", fake_ref)
        body: dict[str, Any] = {"ref": "youtube:abc123", "quality": "best"}
    else:

        def fake_url(url: str, out: Path | None = None, **kw: Any) -> Any:
            seen.append(kw)
            return _fake_download_result(tmp_path)

        monkeypatch.setattr(service, "download_url", fake_url)
        body = {"url": url, "quality": "best"}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])

    assert job["status"] == "done", job
    assert seen[0]["quality"] == "best"


@pytest.mark.parametrize("quality", ["999", "720p", 720])
def test_download_bad_quality_is_422(client: TestClient, quality: Any) -> None:
    body = {"url": "https://www.youtube.com/watch?v=abc123", "quality": quality}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 422
    assert client.get("/api/jobs").json() == []


def test_settings_lists_qualities(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert body["qualities"] == ["best", "2160", "1440", "1080", "720", "480", "360"]


def test_library_keyout_runs_job_and_records_webm(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage.download import service as dl

    calls: list[dict[str, Any]] = []

    def fake_key_out(src: Path, **kwargs: Any) -> Path:
        calls.append({"src": src, **kwargs})
        kwargs["progress"]({"status": "downloading", "downloaded_bytes": None, "fraction": 0.5})
        out = src.with_name(src.stem + ".key.webm")
        out.write_bytes(b"webm")
        return out

    monkeypatch.setattr(dl, "key_out", fake_key_out)
    history_id, path = record_library_file(tmp_path)

    response = client.post(
        f"/api/library/{history_id}/keyout", json={"tolerance": 60}, headers=HEADERS
    )

    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["kind"] == "keyout"
    job = wait_for_job(client, accepted["job_id"])
    assert job["status"] == "done", job
    assert job["kind"] == "keyout"
    assert job["ref"] == f"#{history_id}"
    assert job["path"].endswith(".key.webm")
    assert job["path"] == str(path.with_name("rain clip.key.webm"))
    assert calls[0]["src"] == path
    assert calls[0]["tolerance"] == 60
    new_id = job["history_id"]
    assert new_id is not None and new_id != history_id
    sidecar = client.get(f"/api/library/{new_id}/sidecar")
    assert sidecar.status_code == 200
    meta = sidecar.json()
    assert meta["format"] == "webm"
    assert meta["derived_from"] == "rain clip.mp4"
    assert meta["license"] == "CC0-1.0"
    assert meta["chroma"]["tolerance"] == 60
    rows = client.get("/api/history").json()
    assert rows[0]["id"] == new_id
    assert rows[0]["path"] == job["path"]


def test_library_keyout_requires_header(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path)
    response = client.post(f"/api/library/{history_id}/keyout", json={})
    assert response.status_code == 403
    assert client.get("/api/jobs").json() == []


def test_library_keyout_unknown_id_is_404(client: TestClient, tmp_path: Path) -> None:
    record_library_file(tmp_path)
    response = client.post("/api/library/9999/keyout", json={}, headers=HEADERS)
    assert response.status_code == 404


def test_library_keyout_bad_tolerance_is_422(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path)
    response = client.post(
        f"/api/library/{history_id}/keyout", json={"tolerance": 200}, headers=HEADERS
    )
    assert response.status_code == 422
    assert client.get("/api/jobs").json() == []


def test_library_keyout_outside_library_is_403(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config.set_setting("library", "dir", str(elsewhere))
    response = client.post(f"/api/library/{history_id}/keyout", json={}, headers=HEADERS)
    assert response.status_code == 403


def test_library_keyout_non_video_is_400(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path, name="rain.mp3")
    response = client.post(f"/api/library/{history_id}/keyout", json={}, headers=HEADERS)
    assert response.status_code == 400


def test_library_keyout_already_keyed_is_400(client: TestClient, tmp_path: Path) -> None:
    history_id, _ = record_library_file(tmp_path, name="rain clip.key.webm")
    response = client.post(f"/api/library/{history_id}/keyout", json={}, headers=HEADERS)
    assert response.status_code == 400
    assert "already a keyed export (.key.webm)" in response.json()["detail"]
    assert client.get("/api/jobs").json() == []


class AudioProvider(FakeProvider):
    """Audio results whose ids depend on query and kind; mimics a youtube-style license."""

    kinds = frozenset({Kind.AUDIO_MUSIC, Kind.AUDIO_SFX})

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id)
        self.calls: list[tuple[str, Kind, SearchFilters]] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.calls.append((query, kind, filters))
        return [
            Asset(
                provider=self.id,
                id=f"{kind.value}-{query}",
                kind=kind,
                title=f"{query} ({kind.value})",
                source_url=f"https://example.com/{self.id}/{kind.value}/{query}",
                license="unknown",
                attribution_text="third party",
                commercial_ok=None,
            )
        ]


def test_search_returns_rights_expanded_queries_and_related(client: TestClient) -> None:
    fake = AudioProvider("audiofake")
    registry.register(fake)
    params = {"q": "undertale bgm", "kind": "audio_music", "provider": "audiofake", "limit": "5"}
    response = client.get("/api/search", params={**params, "no_cache": "true"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "undertale soundtrack" in body["expanded_queries"]
    refs = [a["ref"] for a in body["assets"]]
    assert refs[0] == "audiofake:audio_music-undertale bgm"
    assert "audiofake:audio_music-undertale soundtrack" in refs
    assert body["related_kind"] == "audio_sfx"
    # Related searches use the core query ("bgm" stripped) plus the one-word sfx variant.
    assert [a["ref"] for a in body["related"]] == [
        "audiofake:audio_sfx-undertale",
        "audiofake:audio_sfx-undertale sound effect",
    ]
    for asset in body["assets"] + body["related"]:
        assert set(asset["rights"]) == {"level", "label", "detail"}
    # Unknown-license audio titled like a soundtrack rip is flagged; a plain title is unknown.
    assert body["assets"][0]["rights"]["level"] == "copyrighted"
    assert body["assets"][0]["rights"]["label"] == "Likely copyrighted (soundtrack upload)"
    assert body["related"][0]["rights"]["level"] == "unknown"
    related_queries = [q for q, k, _ in fake.calls if k == Kind.AUDIO_SFX]
    assert related_queries == ["undertale", "undertale sound effect"]


def test_search_rights_for_cc0_asset(client: TestClient) -> None:
    registry.register(FixedProvider("aa_first", 1))
    params = {"q": "cat", "kind": "gif", "limit": "1", "no_cache": "true"}
    body = client.get("/api/search", params=params).json()
    assert body["assets"][0]["rights"]["level"] == "clear"
    assert body["assets"][0]["rights"]["label"] == "Free to use (CC0-1.0)"
    assert body["related"] == [] and body["related_kind"] is None


def test_search_expand_and_related_can_be_disabled(client: TestClient) -> None:
    fake = AudioProvider("audiofake")
    registry.register(fake)
    params = {
        "q": "undertale bgm",
        "kind": "audio_music",
        "provider": "audiofake",
        "no_cache": "true",
        "expand": "false",
        "related": "false",
    }
    body = client.get("/api/search", params=params).json()
    assert body["expanded_queries"] == []
    assert body["related"] == [] and body["related_kind"] is None
    assert [(q, k) for q, k, _ in fake.calls] == [("undertale bgm", Kind.AUDIO_MUSIC)]


def test_recall_endpoint(client: TestClient) -> None:
    response = client.get("/api/recall", params={"q": "dog in burning room"})
    assert response.status_code == 200, response.text
    assert response.json() == {"suggestions": ["this is fine"]}
    body = client.get("/api/recall", params={"q": "undertale sans", "kind": "audio_music"}).json()
    assert "megalovania" in body["suggestions"]
    assert client.get("/api/recall").json() == {"suggestions": []}
    assert client.get("/api/recall", params={"q": "x", "kind": "nope"}).status_code == 422


# --- cleared_only ------------------------------------------------------------------------------


class MixedRightsProvider(FakeProvider):
    """GIF results with one license per asset: unknown, NC, CC0, CC BY, all rights reserved."""

    kinds = frozenset({Kind.GIF, Kind.AUDIO_MUSIC, Kind.AUDIO_SFX})
    LICENSES = ("unknown", "CC BY-NC 4.0", "CC0-1.0", "CC BY 4.0", "All rights reserved")

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        return [
            Asset(
                provider=self.id,
                id=f"{kind.value}-{i}",
                kind=kind,
                title=f"clip {i}",
                source_url=f"https://example.com/{self.id}/{kind.value}/{i}",
                license=license_,
                attribution_text="someone",
                commercial_ok=None,
            )
            for i, license_ in enumerate(self.LICENSES)
        ]


def test_search_cleared_only_filters_assets_and_related(client: TestClient) -> None:
    registry.register(MixedRightsProvider("mixed"))
    params = {"q": "rain", "kind": "audio_music", "provider": "mixed", "no_cache": "true"}
    body = client.get("/api/search", params={**params, "limit": "2"}).json()
    assert {a["rights"]["level"] for a in body["assets"]} == {"unknown", "noncommercial"}

    body = client.get("/api/search", params={**params, "limit": "2", "cleared_only": "true"})
    data = body.json()
    # Filtered before truncation: the limit is filled with cleared items, not emptied.
    assert [a["ref"] for a in data["assets"]] == ["mixed:audio_music-2", "mixed:audio_music-3"]
    assert data["related_kind"] == "audio_sfx"
    assert [a["ref"] for a in data["related"]] == ["mixed:audio_sfx-2", "mixed:audio_sfx-3"]
    for asset in data["assets"] + data["related"]:
        assert asset["rights"]["level"] in ("clear", "attribution")


# --- assist ------------------------------------------------------------------------------------


def _assist_result(**overrides: Any) -> Any:
    from clipforage.models import AssistResult

    fields: dict[str, Any] = {
        "queries": ["megalovania", "undertale sans battle"],
        "names": ["Megalovania"],
        "kind_hint": Kind.AUDIO_MUSIC,
        "notes": "Sans boss theme from Undertale.",
        "rights_note": "Copyrighted game music.",
        "backend": "claude_cli",
        "model": "claude-opus-5-5",
        "cost_usd": 0.012,
    }
    fields.update(overrides)
    return AssistResult(**fields)


def test_assist_post_returns_contract_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage import assist

    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_describe(text: str, **kwargs: Any) -> Any:
        seen.append((text, kwargs))
        return _assist_result()

    monkeypatch.setattr(assist, "describe", fake_describe)
    body = {"text": "skeleton battle song", "kind": "audio_music", "style": "meme"}
    response = client.post("/api/assist", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "queries": ["megalovania", "undertale sans battle"],
        "names": ["Megalovania"],
        "kind_hint": "audio_music",
        "notes": "Sans boss theme from Undertale.",
        "rights_note": "Copyrighted game music.",
        "backend": "claude_cli",
        "model": "claude-opus-5-5",
        "cost_usd": 0.012,
    }
    assert seen == [
        ("skeleton battle song", {"kind": Kind.AUDIO_MUSIC, "style": "meme", "backend": None})
    ]


def test_assist_post_requires_header(client: TestClient) -> None:
    assert client.post("/api/assist", json={"text": "cat"}).status_code == 403


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("AssistDisabledError", 403),
        ("AssistUnavailableError", 503),
        ("AssistError", 502),
    ],
)
def test_assist_post_error_statuses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: str, status: int
) -> None:
    from clipforage import assist

    def fake_describe(text: str, **kwargs: Any) -> Any:
        raise getattr(assist, error)(f"{error}: details and enable hints")

    monkeypatch.setattr(assist, "describe", fake_describe)
    response = client.post("/api/assist", json={"text": "cat"}, headers=HEADERS)
    assert response.status_code == status
    assert response.json() == {"detail": f"{error}: details and enable hints"}


def test_assist_post_disabled_by_default_never_runs_a_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage.assist import anthropic_api, base, claude_cli, openai_api

    monkeypatch.delenv("CLIPFORAGE_ASSIST_ENABLED", raising=False)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no backend may run while assist is disabled")

    for module in (claude_cli, anthropic_api, openai_api):
        monkeypatch.setattr(module, "run", boom)
    response = client.post("/api/assist", json={"text": "cat"}, headers=HEADERS)
    assert response.status_code == 403
    assert response.json() == {"detail": base.DISABLED_MESSAGE}


@pytest.mark.parametrize(
    "body",
    [
        {"text": "cat", "kind": "meme"},
        {"text": "cat", "style": "vaporwave"},
        {"text": "cat", "backend": "gemini"},
        {"text": "   "},
        {},
    ],
)
def test_assist_post_bad_input_is_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    from clipforage import assist

    monkeypatch.setattr(assist, "describe", lambda *a, **k: pytest.fail("must not be called"))
    assert client.post("/api/assist", json=body, headers=HEADERS).status_code == 422


def test_assist_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforage import assist
    from clipforage.assist.base import BackendStatus

    monkeypatch.delenv("CLIPFORAGE_ASSIST_ENABLED", raising=False)
    monkeypatch.delenv("CLIPFORAGE_ASSIST_BACKEND", raising=False)
    statuses = [
        BackendStatus(id="claude_cli", available=False, detail="not found", model="claude-x"),
        BackendStatus(id="anthropic", available=True, detail="key", model="claude-y"),
        BackendStatus(id="openai", available=True, detail="key", model="gpt-4o-mini"),
    ]
    monkeypatch.setattr(assist, "available_backends", lambda: statuses)
    body = client.get("/api/assist/status").json()
    assert body == {
        "backends": [s.model_dump() for s in statuses],
        "configured": "auto",
        "auto_pick": "anthropic",
        "enabled": False,
    }
    monkeypatch.setenv("CLIPFORAGE_ASSIST_ENABLED", "true")
    monkeypatch.setattr(
        assist,
        "available_backends",
        lambda: [s.model_copy(update={"available": False}) for s in statuses],
    )
    body = client.get("/api/assist/status").json()
    assert body["auto_pick"] is None and body["enabled"] is True


def test_settings_assist_block_and_round_trip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in (
        "CLIPFORAGE_ASSIST_ENABLED",
        "CLIPFORAGE_ASSIST_BACKEND",
        "CLIPFORAGE_ASSIST_MODEL",
        "CLIPFORAGE_ASSIST_CLAUDE_CLI_MODEL",
        "CLIPFORAGE_ASSIST_ANTHROPIC_MODEL",
        "CLIPFORAGE_ASSIST_OPENAI_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CLIPFORAGE_ANTHROPIC_KEY", raising=False)
    monkeypatch.delenv("CLIPFORAGE_OPENAI_KEY", raising=False)
    assist_block = client.get("/api/settings").json()["assist"]
    assert assist_block == {
        "enabled": False,
        "backend": "auto",
        "model": None,
        "claude_cli_model": None,
        "anthropic_model": None,
        "openai_model": None,
        "keys": {"anthropic": None, "openai": None},
    }

    body = {"enabled": True, "backend": "openai", "model": "gpt-5"}
    response = client.put("/api/settings/assist", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["assist"]["enabled"] is True
    assert out["assist"]["backend"] == "openai"
    assert out["assist"]["model"] == "gpt-5"
    assert "library_dir" in out and "providers" in out  # the whole settings object
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert data["assist"] == {"enabled": True, "backend": "openai", "model": "gpt-5"}

    # None leaves fields alone; an empty model clears it.
    response = client.put("/api/settings/assist", json={"model": ""}, headers=HEADERS)
    assert response.json()["assist"] == {
        "enabled": True,
        "backend": "openai",
        "model": None,
        "claude_cli_model": None,
        "anthropic_model": None,
        "openai_model": None,
        "keys": {"anthropic": None, "openai": None},
    }
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert "model" not in data["assist"]
    response = client.put("/api/settings/assist", json={"enabled": False}, headers=HEADERS)
    assert response.json()["assist"]["enabled"] is False
    assert response.json()["assist"]["backend"] == "openai"

    # The UI sends "auto" to go back to the default: the key is removed, not pinned.
    response = client.put("/api/settings/assist", json={"backend": "auto"}, headers=HEADERS)
    assert response.json()["assist"]["backend"] == "auto"
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert data["assist"] == {"enabled": False}


def test_put_assist_settings_bad_backend_is_422(client: TestClient) -> None:
    response = client.put("/api/settings/assist", json={"backend": "gemini"}, headers=HEADERS)
    assert response.status_code == 422
    assert client.put("/api/settings/assist", json={}).status_code == 403  # header guard


def test_put_assist_per_backend_models_round_trip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in (
        "CLIPFORAGE_ASSIST_MODEL",
        "CLIPFORAGE_ASSIST_CLAUDE_CLI_MODEL",
        "CLIPFORAGE_ASSIST_ANTHROPIC_MODEL",
        "CLIPFORAGE_ASSIST_OPENAI_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    body = {
        "model": "gpt-5",
        "claude_cli_model": " sonnet ",
        "anthropic_model": "claude-sonnet-5",
        "openai_model": "gpt-4.1",
    }
    response = client.put("/api/settings/assist", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    out = response.json()["assist"]
    assert out["model"] == "gpt-5"
    assert out["claude_cli_model"] == "sonnet"
    assert out["anthropic_model"] == "claude-sonnet-5"
    assert out["openai_model"] == "gpt-4.1"
    assert client.get("/api/settings").json()["assist"] == out
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert data["assist"]["claude_cli_model"] == "sonnet"
    assert data["assist"]["openai_model"] == "gpt-4.1"

    # None leaves per-backend keys alone; an empty string removes one.
    response = client.put("/api/settings/assist", json={"enabled": True}, headers=HEADERS)
    assert response.json()["assist"]["openai_model"] == "gpt-4.1"
    response = client.put("/api/settings/assist", json={"openai_model": ""}, headers=HEADERS)
    out = response.json()["assist"]
    assert out["openai_model"] is None
    assert out["anthropic_model"] == "claude-sonnet-5"
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert "openai_model" not in data["assist"]

    # The env var wins over the file, as everywhere.
    monkeypatch.setenv("CLIPFORAGE_ASSIST_ANTHROPIC_MODEL", "claude-haiku-5")
    assert client.get("/api/settings").json()["assist"]["anthropic_model"] == "claude-haiku-5"


@pytest.mark.parametrize("key_id", ["anthropic", "openai"])
def test_put_assist_key_masked_and_removed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, key_id: str
) -> None:
    monkeypatch.delenv(f"CLIPFORAGE_{key_id.upper()}_KEY", raising=False)
    url = f"/api/settings/keys/{key_id}"
    response = client.put(url, json={"key": "sk-secret-abcd1234"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"provider": key_id, "key": "sk-s…1234"}
    settings = client.get("/api/settings")
    assert settings.json()["assist"]["keys"][key_id] == "sk-s…1234"
    assert "sk-secret-abcd1234" not in settings.text
    assert key_id not in settings.json()["keys"]  # provider keys stay provider-only

    response = client.put(url, json={"key": ""}, headers=HEADERS)
    assert response.json()["key"] is None
    assert client.get("/api/settings").json()["assist"]["keys"][key_id] is None


# --- JobOut.title ------------------------------------------------------------------------------


def test_download_job_title_from_ref_asset(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage import service

    seed_asset(provider="youtube", id="abc123", kind=Kind.VIDEO, title="Big Buck Bunny")
    monkeypatch.setattr(
        service,
        "download_ref",
        lambda ref, **kw: (
            "https://www.youtube.com/watch?v=abc123",
            _fake_download_result(tmp_path),
        ),
    )
    body = {"ref": "youtube:abc123", "title": "ignored for refs"}
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}").json()["title"] == "Big Buck Bunny"
    job = wait_for_job(client, job_id)
    assert job["title"] == "Big Buck Bunny"
    assert any(j["title"] == "Big Buck Bunny" for j in client.get("/api/jobs").json())


@pytest.mark.parametrize("title", ["My Clip", None])
def test_download_job_title_from_url_request(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, title: str | None
) -> None:
    from clipforage import service

    monkeypatch.setattr(
        service, "download_url", lambda url, out=None, **kw: _fake_download_result(tmp_path)
    )
    body: dict[str, Any] = {"url": "https://www.youtube.com/watch?v=abc123"}
    if title is not None:
        body["title"] = title
    response = client.post("/api/download", json=body, headers=HEADERS)
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["job_id"])
    assert job["title"] == title


class MixedProvider(FakeProvider):
    """One clean and one explicit GIF result."""

    kinds = frozenset({Kind.GIF})

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        return [
            Asset(
                provider=self.id,
                id=asset_id,
                kind=kind,
                title=title,
                source_url=f"https://example.com/{asset_id}",
                license="CC0-1.0",
                attribution_text="CC0",
                commercial_ok=True,
            )
            for asset_id, title in (("1", "dancing cat"), ("2", "xxx dancing"))
        ]


def test_search_safe_default_param_and_filtered_count(client: TestClient) -> None:
    registry.register(MixedProvider("mixed"))
    params = {"q": "dance", "kind": "gif", "no_cache": "true", "expand": "false"}
    body = client.get("/api/search", params=params).json()
    assert [a["ref"] for a in body["assets"]] == ["mixed:1"]
    assert body["filtered_count"] == 1
    body = client.get("/api/search", params={**params, "safe": "false"}).json()
    assert [a["ref"] for a in body["assets"]] == ["mixed:1", "mixed:2"]
    assert body["filtered_count"] == 0
    # safe omitted follows [search] safe; an explicit safe=true overrides it.
    config.set_setting("search", "safe", False)
    assert client.get("/api/search", params=params).json()["filtered_count"] == 0
    body = client.get("/api/search", params={**params, "safe": "true"}).json()
    assert body["filtered_count"] == 1


def test_settings_search_round_trip(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPFORAGE_SAFE_SEARCH", raising=False)
    assert client.get("/api/settings").json()["search"] == {"safe": True}
    response = client.put("/api/settings/search", json={"safe": False}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["search"] == {"safe": False}
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert data["search"] == {"safe": False}
    assert client.get("/api/settings").json()["search"] == {"safe": False}
    response = client.put("/api/settings/search", json={"safe": None}, headers=HEADERS)
    assert response.json()["search"] == {"safe": True}  # null removes the key: default on
    data = tomllib.loads(config.config_path().read_text(encoding="utf-8"))
    assert "safe" not in data.get("search", {})


def test_settings_search_put_requires_csrf_header(client: TestClient) -> None:
    assert client.put("/api/settings/search", json={"safe": False}).status_code == 403


def test_search_response_exposes_relaxed_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage import service
    from clipforage.models import SearchOutcome

    monkeypatch.setattr(
        service,
        "search_expanded",
        lambda *a, **k: SearchOutcome(assets=[], errors=[], relaxed_query="phonk"),
    )
    params = {"q": "phonk background music", "kind": "audio_music", "cleared_only": "true"}
    assert client.get("/api/search", params=params).json()["relaxed_query"] == "phonk"
    monkeypatch.setattr(service, "search_expanded", lambda *a, **k: SearchOutcome([], []))
    assert client.get("/api/search", params=params).json()["relaxed_query"] is None
