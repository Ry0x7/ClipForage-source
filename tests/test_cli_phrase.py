from collections.abc import Sequence
from typing import Any

import pytest
from typer.testing import CliRunner

from clipforage import service
from clipforage.cli import app
from clipforage.download.service import parse_clip
from clipforage.models import Asset, Kind, PhraseHit, PhraseOutcome
from clipforage.providers.base import ProviderError

runner = CliRunner()


def make_hit(provider: str, asset_id: str, start: float, end: float, text: str) -> PhraseHit:
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
    return PhraseHit(asset=asset, start_s=start, end_s=end, text=text, lang="en")


class FakePhraseSearch:
    def __init__(
        self,
        hits: list[PhraseHit],
        errors: list[ProviderError] | None = None,
        exc: Exception | None = None,
        warnings: Sequence[str] = (),
        filtered_count: int = 0,
    ) -> None:
        self.hits = hits
        self.errors = errors or []
        self.exc = exc
        self.warnings = warnings
        self.filtered_count = filtered_count
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, query: str, **kwargs: Any) -> PhraseOutcome:
        self.calls.append((query, kwargs))
        if self.exc is not None:
            raise self.exc
        for message in self.warnings:
            kwargs["warn"](message)
        return PhraseOutcome(self.hits, self.errors, self.filtered_count)


def install(monkeypatch: pytest.MonkeyPatch, fake: FakePhraseSearch) -> FakePhraseSearch:
    monkeypatch.setattr(service, "phrase_search", fake)
    return fake


def test_output_format(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        FakePhraseSearch(
            [
                make_hit("youtube", "dQw4w9WgXcQ", 42.6, 45.2, "Never gonna give you up"),
                make_hit("internet_archive", "night_1968", 3723.4, 3726.0, "They're coming"),
            ]
        ),
    )
    result = runner.invoke(app, ["phrase", "never gonna give you up"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "  1  youtube     0:42-0:45  Title dQw4w9WgXcQ",
        (
            "     youtube:dQw4w9WgXcQ  https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s  "
            '"Never gonna give you up"'
        ),
        "     get youtube:dQw4w9WgXcQ --clip 41.6-46.2",
        "  2  internet_archive 1:02:03-1:02:06  Title night_1968",
        (
            "     internet_archive:night_1968  https://archive.org/details/night_1968  "
            '"They\'re coming"'
        ),
        "     get internet_archive:night_1968 --clip 3722.4-3727.0",
        "Quote lookup (opens in browser, not searched): PlayPhrase",
    ]


def test_printed_clip_spec_is_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakePhraseSearch([make_hit("youtube", "abc", 0.3, 2.0, "hi")]))
    result = runner.invoke(app, ["phrase", "hi"])
    spec = result.stdout.splitlines()[2].rsplit(" ", 1)[-1]
    assert spec == "0.0-3.0"
    assert parse_clip(spec) == (0.0, 3.0)


def test_options_are_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakePhraseSearch([]))
    result = runner.invoke(
        app,
        [
            "phrase",
            "hello there",
            "--source",
            "youtube",
            "--source",
            "internet_archive",
            "--within",
            "https://www.youtube.com/playlist?list=PL1",
            "--max-videos",
            "3",
            "--limit",
            "5",
            "--no-cache",
            "--max-per-video",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    query, kwargs = fake.calls[0]
    assert query == "hello there"
    assert kwargs["sources"] == ["youtube", "internet_archive"]
    assert kwargs["within"] == "https://www.youtube.com/playlist?list=PL1"
    assert kwargs["max_videos"] == 3
    assert kwargs["limit"] == 5
    assert kwargs["use_cache"] is False
    assert kwargs["max_per_video"] == 0


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakePhraseSearch([]))
    result = runner.invoke(app, ["phrase", "hello"])
    assert result.exit_code == 0
    _, kwargs = fake.calls[0]
    assert kwargs["sources"] is None
    assert kwargs["within"] is None
    assert kwargs["max_videos"] == 10
    assert kwargs["limit"] == 20
    assert kwargs["use_cache"] is True
    assert kwargs["max_per_video"] == 3
    assert result.stdout.splitlines() == [
        "no hits",
        "Quote lookup (opens in browser, not searched): PlayPhrase",
    ]


def test_warnings_and_errors_go_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        FakePhraseSearch(
            [make_hit("youtube", "abc", 1.0, 2.0, "hi")],
            errors=[ProviderError("internet_archive", "HTTP 503: down")],
            warnings=["youtube:xyz: could not fetch captions: boom"],
        ),
    )
    result = runner.invoke(app, ["phrase", "hi"])
    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        "warning: youtube:xyz: could not fetch captions: boom",
        "warning: internet_archive: HTTP 503: down",
    ]
    assert "warning" not in result.stdout


def test_unknown_source_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakePhraseSearch([], exc=LookupError("unknown phrase source 'x'")))
    result = runner.invoke(app, ["phrase", "hi", "--source", "x"])
    assert result.exit_code == 1
    assert result.stderr.strip() == "error: unknown phrase source 'x'"


def test_unknown_source_real_service() -> None:
    result = runner.invoke(app, ["phrase", "hi", "--source", "pexels"])
    assert result.exit_code == 1
    assert result.stderr.startswith("error: unknown phrase source 'pexels'")


def test_cache_unavailable_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = service.CacheUnavailableError("cache unavailable: disk I/O error")
    install(monkeypatch, FakePhraseSearch([], exc=exc))
    result = runner.invoke(app, ["phrase", "hi"])
    assert result.exit_code == 1
    assert result.stderr.strip() == "error: cache unavailable: disk I/O error"


def test_long_title_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = make_hit("youtube", "abc", 1.0, 2.0, "hi")
    hit = hit.model_copy(update={"asset": hit.asset.model_copy(update={"title": "x" * 200})})
    install(monkeypatch, FakePhraseSearch([hit]))
    first = runner.invoke(app, ["phrase", "hi"]).stdout.splitlines()[0]
    assert len(first) == 100
    assert first.endswith("...")


def test_cache_clear_reports_captions(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforage.cache import Cache

    with Cache() as cache:
        cache.put_captions(
            make_hit("youtube", "a", 1, 2, "x").asset, [], lang=None, auto_generated=False
        )
    result = runner.invoke(app, ["cache-clear"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "cleared 0 cached searches",
        "cleared captions of 1 videos",
    ]
    with Cache() as cache:
        assert cache.has_captions("youtube", "a") is False


def test_cache_clear_reports_zero_captions() -> None:
    result = runner.invoke(app, ["cache-clear"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "cleared 0 cached searches",
        "cleared captions of 0 videos",
    ]


def test_unsafe_flag_turns_safe_search_off(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakePhraseSearch([]))
    assert runner.invoke(app, ["phrase", "hello"]).exit_code == 0
    assert fake.calls[0][1]["safe"] is None  # None: the [search] safe default
    assert runner.invoke(app, ["phrase", "hello", "--unsafe"]).exit_code == 0
    assert fake.calls[1][1]["safe"] is False


def test_hidden_by_safe_search_line(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakePhraseSearch([], filtered_count=2))
    result = runner.invoke(app, ["phrase", "hello"])
    assert result.exit_code == 0
    assert "2 result(s) hidden by safe search (--unsafe to show)" in result.output
    install(monkeypatch, FakePhraseSearch([]))
    assert "hidden by safe search" not in runner.invoke(app, ["phrase", "hello"]).output
