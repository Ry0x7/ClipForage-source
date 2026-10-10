from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipforage.models import Asset, DownloadPlan, DownloadResult, Kind, SearchFilters


def make_asset(**overrides: object) -> Asset:
    data: dict[str, object] = {
        "provider": "fake",
        "id": "1",
        "kind": Kind.VIDEO,
        "title": "Green screen cat",
        "source_url": "https://example.com/1",
        "license": "CC0-1.0",
        "attribution_text": "Cat by Someone",
        "commercial_ok": True,
    }
    data.update(overrides)
    return Asset(**data)


def test_kind_has_exactly_ten_values() -> None:
    assert len(Kind) == 10
    assert {k.value for k in Kind} == {
        "video",
        "image",
        "gif",
        "sticker",
        "audio_sfx",
        "audio_music",
        "font",
        "lut",
        "overlay",
        "template",
    }


def test_asset_defaults() -> None:
    asset = make_asset()
    assert asset.preview_url is None
    assert asset.media_url is None
    assert asset.media_type is None
    assert asset.duration_s is None
    assert asset.orientation is None
    assert asset.extra == {}


def test_asset_json_without_media_fields_validates() -> None:
    # Cached rows written before media_url/media_type existed must still load.
    payload = (
        '{"provider": "fake", "id": "1", "kind": "video", "title": "t",'
        ' "source_url": "https://example.com/1", "preview_url": "https://example.com/1.jpg",'
        ' "license": "CC0-1.0", "attribution_text": "a", "commercial_ok": true}'
    )
    asset = Asset.model_validate_json(payload)
    assert asset.preview_url == "https://example.com/1.jpg"
    assert asset.media_url is None
    assert asset.media_type is None


def test_asset_is_frozen() -> None:
    asset = make_asset()
    with pytest.raises(ValidationError):
        asset.title = "changed"  # type: ignore[misc]


def test_filters_are_frozen() -> None:
    filters = SearchFilters(kind=Kind.GIF)
    with pytest.raises(ValidationError):
        filters.commercial_only = True  # type: ignore[misc]


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_license_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        make_asset(license=value)


def test_license_and_commercial_ok_required() -> None:
    with pytest.raises(ValidationError):
        Asset(  # type: ignore[call-arg]
            provider="fake",
            id="1",
            kind=Kind.VIDEO,
            title="t",
            source_url="https://example.com",
            attribution_text="a",
        )


def test_commercial_ok_accepts_none() -> None:
    assert make_asset(commercial_ok=None).commercial_ok is None


def test_invalid_orientation_rejected() -> None:
    with pytest.raises(ValidationError):
        make_asset(orientation="diagonal")


def test_download_plan_and_result() -> None:
    plan = DownloadPlan(
        asset=make_asset(), method="http", url="https://example.com/1.mp4", ext="mp4"
    )
    assert plan.headers == {}
    assert plan.ytdlp_opts == {}
    with pytest.raises(ValidationError):
        DownloadPlan(asset=make_asset(), method="ftp", url="x", ext="mp4")  # type: ignore[arg-type]
    result = DownloadResult(
        path=Path("a.mp4"), sidecar=Path("a.mp4.json"), fetched_at=datetime.now(UTC)
    )
    assert result.sidecar.name == "a.mp4.json"
