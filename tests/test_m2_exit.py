"""M2 exit criteria: pexels + pixabay through the registry and the CLI, no live network."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from clipforage.cache import Cache
from clipforage.cli import app
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers import pexels, pixabay, registry

FIXTURES = Path(__file__).parent / "fixtures"
FILTERS = SearchFilters(kind=Kind.VIDEO, orientation="landscape", commercial_only=True)
runner = CliRunner()


def load(provider: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / provider / name).read_text(encoding="utf-8"))


class FakeProvider:
    """Stands in for youtube (no network) or returns fixed assets."""

    kinds = frozenset({Kind.VIDEO})
    requires_key = False

    def __init__(self, provider_id: str, assets: list[Asset] | None = None) -> None:
        self.id = provider_id
        self.assets = assets or []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        return list(self.assets)

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="http", url=asset.source_url, ext="mp4")


@pytest.fixture(autouse=True)
def _keys_and_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_PEXELS_KEY", "test-pexels-key")
    monkeypatch.setenv("CLIPFORAGE_PIXABAY_KEY", "test-pixabay-key")
    registry.register(FakeProvider("youtube"))  # load_builtins then skips the real one
    # Other built-ins that search video would hit the network; stand in with empty fakes.
    for provider_id in ("wikimedia", "internet_archive", "mixkit"):
        registry.register(FakeProvider(provider_id))
    registry.load_builtins()


def mock_apis(pixabay_status: int = 200) -> None:
    respx.get(pexels.VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load("pexels", "videos_search.json"))
    )
    if pixabay_status == 200:
        response = httpx.Response(200, json=load("pixabay", "videos_search.json"))
    else:
        response = httpx.Response(pixabay_status, text="server error")
    respx.get(pixabay.VIDEOS_URL).mock(return_value=response)


@respx.mock
def test_search_all_returns_both_providers_with_license_fields() -> None:
    mock_apis()
    assets, errors = registry.search_all("rain window", FILTERS)
    assert errors == []
    assert {a.provider for a in assets} >= {"pexels", "pixabay"}
    for asset in assets:
        assert asset.license
        assert asset.attribution_text
        assert asset.source_url
        assert asset.commercial_ok is not None
        assert asset.orientation in (None, "landscape")


@respx.mock
def test_one_provider_failing_keeps_the_other() -> None:
    mock_apis(pixabay_status=500)
    assets, errors = registry.search_all("rain window", FILTERS)
    assert {a.provider for a in assets} == {"pexels"}
    assert len(errors) == 1
    assert errors[0].provider == "pixabay"
    assert "500" in errors[0].message


def test_commercial_only_excludes_noncommercial_and_unknown() -> None:
    def asset(asset_id: str, license_: str, commercial_ok: bool | None) -> Asset:
        return Asset(
            provider="fake",
            id=asset_id,
            kind=Kind.VIDEO,
            title=asset_id,
            source_url=f"https://example.com/{asset_id}",
            license=license_,
            attribution_text="someone",
            commercial_ok=commercial_ok,
        )

    fake = FakeProvider(
        "fake",
        [
            asset("ok", "CC0-1.0", True),
            asset("nc", "CC-BY-NC-4.0", False),
            asset("u", "unknown", None),
        ],
    )
    found, _ = registry.search_all("q", FILTERS, providers=[fake])
    assert [a.id for a in found] == ["ok"]


@respx.mock
def test_cli_search_prints_refs_for_both_providers() -> None:
    mock_apis()
    result = runner.invoke(
        app,
        [
            "search",
            "rain window",
            "--kind",
            "video",
            "--orientation",
            "landscape",
            "--commercial-only",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "pexels:3571264" in result.output
    assert "pixabay:125" in result.output
    assert "pexels:5752729" not in result.output  # portrait, filtered out


@respx.mock
def test_cli_get_ref_downloads_cached_asset(tmp_path: Path) -> None:
    mock_apis()
    search = runner.invoke(
        app, ["search", "rain window", "--orientation", "landscape", "--commercial-only"]
    )
    assert search.exit_code == 0, search.output
    with Cache() as cache:
        asset = cache.find_asset("pexels", "3571264")
    assert asset is not None
    media = respx.get(asset.extra["download_url"]).mock(
        return_value=httpx.Response(200, content=b"fake mp4 bytes")
    )

    out = tmp_path / "library"
    result = runner.invoke(app, ["get", "pexels:3571264", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert media.call_count == 1
    files = sorted(p.name for p in out.iterdir())
    assert files == [
        "Pexels video 3571264 by Joey Farina [3571264].mp4",
        "Pexels video 3571264 by Joey Farina [3571264].mp4.json",
    ]
    assert (out / files[0]).read_bytes() == b"fake mp4 bytes"
    meta = json.loads((out / files[1]).read_text(encoding="utf-8"))
    assert meta["provider"] == "pexels"
    assert meta["license"] == "Pexels License"
    assert "license  Pexels License" in result.output
    assert "Video by Joey Farina on Pexels" in result.output
    with Cache() as cache:
        rows = cache.history()
    assert rows[0]["provider"] == "pexels"
    assert rows[0]["asset_id"] == "3571264"
    assert rows[0]["url"] == asset.extra["download_url"]


def test_cli_get_uncached_ref_exits_1_with_hint() -> None:
    result = runner.invoke(app, ["get", "pexels:nonexistent"])
    assert result.exit_code == 1
    assert "run a search first so the asset is cached" in result.output


def test_cli_get_rejects_unknown_ref() -> None:
    result = runner.invoke(app, ["get", "nope:123"])
    assert result.exit_code == 1
    assert "not a URL" in result.output
