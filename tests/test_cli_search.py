import io

import pytest
from typer.testing import CliRunner

from clipforage.cli import _echo_search_errors, _make_encodable, app
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers import registry
from clipforage.providers.base import ProviderError

runner = CliRunner()


def _asset(provider: str, asset_id: str, **kwargs: object) -> Asset:
    fields: dict[str, object] = {
        "provider": provider,
        "id": asset_id,
        "kind": Kind.VIDEO,
        "title": f"Title {asset_id}",
        "source_url": f"https://example.com/{asset_id}",
        "license": "unknown",
        "attribution_text": "attr",
        "commercial_ok": None,
    }
    fields.update(kwargs)
    return Asset(**fields)  # type: ignore[arg-type]


class FakeProvider:
    kinds = frozenset({Kind.VIDEO})
    requires_key = False

    def __init__(self, provider_id: str, assets: list[Asset], fail: bool = False) -> None:
        self.id = provider_id
        self.assets = assets
        self.fail = fail

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        if self.fail:
            raise ProviderError(self.id, "service down")
        return self.assets

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="http", url=asset.source_url, ext="mp4")


@pytest.fixture(autouse=True)
def _no_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "load_builtins", lambda: None)


def _register_two() -> None:
    registry.register(
        FakeProvider(
            "youtube",
            [_asset("youtube", "yt1", title="Green Screen Cat", duration_s=75.0)],
        )
    )
    registry.register(
        FakeProvider(
            "stock",
            [_asset("stock", "st1", license="CC0-1.0", commercial_ok=True, duration_s=None)],
        )
    )


def test_search_lists_results_with_license() -> None:
    _register_two()

    result = runner.invoke(app, ["search", "green screen"])

    assert result.exit_code == 0, result.output
    assert "unknown (third-party)" in result.output
    assert "CC0-1.0" in result.output
    assert "1:15" in result.output
    assert "Green Screen Cat" in result.output
    assert "https://example.com/yt1" in result.output
    assert "https://example.com/st1" in result.output


def test_search_failing_provider_warns_and_exits_zero() -> None:
    _register_two()
    registry.register(FakeProvider("broken", [], fail=True))

    result = runner.invoke(app, ["search", "q"])

    assert result.exit_code == 0, result.output
    assert "warning: broken: service down" in result.output
    assert "Green Screen Cat" in result.output


class KeyedFakeProvider(FakeProvider):
    requires_key = True


def test_search_missing_key_is_one_hint_not_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    _register_two()
    monkeypatch.setattr(registry, "has_key", lambda provider_id: False)
    registry.register(KeyedFakeProvider("pexels", [_asset("pexels", "px1")]))

    result = runner.invoke(app, ["search", "q"])

    assert result.exit_code == 0, result.output
    assert "warning: pexels" not in result.output
    hint = "pexels skipped: no API key (set CLIPFORAGE_PEXELS_KEY)"
    assert result.output.count(hint) == 1
    assert result.output.count("pexels") == 1
    assert "Green Screen Cat" in result.output


def test_echo_search_errors_hints_once_per_provider(capsys: pytest.CaptureFixture[str]) -> None:
    _echo_search_errors(
        [
            ProviderError("pexels", "no API key; set CLIPFORAGE_PEXELS_KEY or add it to x"),
            ProviderError("pexels", "missing API key (set CLIPFORAGE_PEXELS_KEY)"),
            ProviderError("broken", "service down"),
        ]
    )

    err = capsys.readouterr().err
    assert err.count("pexels skipped: no API key (set CLIPFORAGE_PEXELS_KEY)") == 1
    assert err.count("warning:") == 1
    assert "warning: broken: service down" in err


def test_search_limit() -> None:
    _register_two()

    result = runner.invoke(app, ["search", "q", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert result.output.count("https://example.com/") == 1


def test_search_bad_kind() -> None:
    result = runner.invoke(app, ["search", "q", "--kind", "banana"])

    assert result.exit_code == 1
    assert "error: invalid kind" in result.output
    assert "Traceback" not in result.output


def test_search_commercial_only_drops_unknown() -> None:
    _register_two()

    result = runner.invoke(app, ["search", "q", "--commercial-only"])

    assert result.exit_code == 0, result.output
    assert "https://example.com/st1" in result.output
    assert "https://example.com/yt1" not in result.output


def test_search_no_results() -> None:
    result = runner.invoke(app, ["search", "q", "--kind", "image"])

    assert result.exit_code == 0
    assert "no results" in result.output


def test_search_unknown_provider() -> None:
    _register_two()

    result = runner.invoke(app, ["search", "q", "--provider", "nope"])

    assert result.exit_code == 1
    assert "error: unknown provider 'nope'" in result.output


def test_search_provider_restricts() -> None:
    _register_two()

    result = runner.invoke(app, ["search", "q", "--provider", "stock"])

    assert result.exit_code == 0, result.output
    assert "https://example.com/st1" in result.output
    assert "https://example.com/yt1" not in result.output


def test_search_bad_orientation() -> None:
    result = runner.invoke(app, ["search", "q", "--orientation", "diagonal"])

    assert result.exit_code == 1
    assert "error: invalid orientation" in result.output


def test_search_no_cache_flag_bypasses_lookup() -> None:
    calls: list[str] = []

    class CountingProvider(FakeProvider):
        def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
            calls.append(query)
            return super().search(query, kind, filters)

    registry.register(CountingProvider("stock", [_asset("stock", "st1")]))

    first = runner.invoke(app, ["search", "q"])
    cached = runner.invoke(app, ["search", "q"])
    fresh = runner.invoke(app, ["search", "q", "--no-cache"])

    for result in (first, cached, fresh):
        assert result.exit_code == 0, result.output
        assert "https://example.com/st1" in result.output
    assert len(calls) == 2


def test_search_prints_linkouts_for_kind() -> None:
    _register_two()

    result = runner.invoke(app, ["search", "q"])

    assert result.exit_code == 0, result.output
    footer = result.output.strip().splitlines()[-1]
    assert footer == (
        "Also try (opens in browser, not searched): Videvo, PlayPhrase, TikTok, Instagram Reels"
    )


def test_search_linkouts_follow_kind() -> None:
    result = runner.invoke(app, ["search", "q", "--kind", "lut"])

    assert result.exit_code == 0, result.output
    assert (
        "Also try (opens in browser, not searched): RocketStock free LUTs, IWLTBAP free LUTs"
        in result.output
    )
    assert "Mixkit" not in result.output


def test_search_no_linkout_line_when_none_for_kind() -> None:
    result = runner.invoke(app, ["search", "q", "--kind", "image"])

    assert result.exit_code == 0, result.output
    assert "Also try" not in result.output


class RecordingProvider(FakeProvider):
    def __init__(self, provider_id: str, assets: list[Asset]) -> None:
        super().__init__(provider_id, assets)
        self.filters: list[SearchFilters] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.filters.append(filters)
        return self.assets


def test_search_style_and_limit_reach_provider() -> None:
    fake = RecordingProvider("youtube", [_asset("youtube", "yt1")])
    registry.register(fake)

    result = runner.invoke(
        app, ["search", "q", "--style", "meme", "--limit", "8", "--provider", "youtube"]
    )

    assert result.exit_code == 0, result.output
    assert fake.filters[-1].style == "meme"
    assert fake.filters[-1].limit == 8


def test_search_style_defaults_to_any() -> None:
    fake = RecordingProvider("youtube", [_asset("youtube", "yt1")])
    registry.register(fake)

    result = runner.invoke(app, ["search", "q", "--no-cache"])

    assert result.exit_code == 0, result.output
    assert fake.filters[-1].style == "any"


def test_search_shorts_reaches_provider() -> None:
    fake = RecordingProvider("youtube", [_asset("youtube", "yt1")])
    registry.register(fake)

    result = runner.invoke(app, ["search", "q", "--shorts", "--provider", "youtube"])

    assert result.exit_code == 0, result.output
    assert fake.filters[-1].shorts is True
    assert runner.invoke(app, ["search", "q", "--no-cache"]).exit_code == 0
    assert fake.filters[-1].shorts is False


def test_search_bad_style() -> None:
    result = runner.invoke(app, ["search", "q", "--style", "vlog"])
    assert result.exit_code == 1
    assert "invalid style 'vlog'" in result.output
    assert "green_screen" in result.output


def test_search_emoji_title_on_cp1252_stdout_does_not_crash() -> None:
    title = "Crying cat 👻 meme"
    registry.register(FakeProvider("youtube", [_asset("youtube", "yt1", title=title)]))

    result = CliRunner(charset="cp1252").invoke(app, ["search", "q", "--no-cache"])

    assert result.exception is None, repr(result.exception)
    assert result.exit_code == 0
    assert "Crying cat ? meme" in result.output


def test_make_encodable_reconfigures_legacy_stream() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    _make_encodable(stream)
    stream.write("a 👻 b")
    stream.flush()
    assert raw.getvalue() == b"a ? b"


def test_make_encodable_leaves_utf8_and_odd_streams_alone() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    _make_encodable(stream)
    assert stream.errors == "strict"
    _make_encodable(None)
    _make_encodable(io.StringIO())  # no usable encoding: nothing to do

    class NoReconfigure:
        encoding = "cp1252"

    _make_encodable(NoReconfigure())  # type: ignore[arg-type]


class QueryProvider(FakeProvider):
    """One asset per (query, kind); records every call."""

    kinds = frozenset({Kind.VIDEO, Kind.AUDIO_MUSIC, Kind.AUDIO_SFX})

    def __init__(self, provider_id: str, license: str = "CC0-1.0") -> None:
        super().__init__(provider_id, [])
        self.license = license
        self.calls: list[tuple[str, Kind]] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.calls.append((query, kind))
        slug = query.replace(" ", "-")
        return [
            _asset(
                self.id,
                f"{kind.value}-{slug}",
                kind=kind,
                title=f"{query} {kind.value}",
                license=self.license,
                commercial_ok=True,
            )
        ]


def test_search_prints_expansions_and_rights_level() -> None:
    fake = QueryProvider("stock")
    registry.register(fake)

    result = runner.invoke(app, ["search", "dog in burning room", "--no-cache"])

    assert result.exit_code == 0, result.output
    assert "also searched: this is fine" in result.output
    assert "stock:video-dog-in-burning-room" in result.output
    assert "stock:video-this-is-fine" in result.output
    assert "[clear]" in result.output


def test_search_youtube_result_is_marked_copyrighted() -> None:
    registry.register(FakeProvider("youtube", [_asset("youtube", "yt1")]))

    result = runner.invoke(app, ["search", "rain", "--no-cache"])

    assert result.exit_code == 0, result.output
    assert "[copyrighted]" in result.output


def test_search_no_expand_flag() -> None:
    fake = QueryProvider("stock")
    registry.register(fake)

    result = runner.invoke(app, ["search", "dog in burning room", "--no-cache", "--no-expand"])

    assert result.exit_code == 0, result.output
    assert "also searched" not in result.output
    assert fake.calls == [("dog in burning room", Kind.VIDEO)]


def test_search_related_audio_section_and_no_related_flag() -> None:
    fake = QueryProvider("stock")
    registry.register(fake)
    args = ["search", "rain", "--kind", "audio_music", "--no-cache", "--no-expand"]

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert "related (audio_sfx):" in result.output
    assert result.output.index("stock:audio_music-rain") < result.output.index("related (")
    assert result.output.index("related (") < result.output.index("stock:audio_sfx-rain")

    fake.calls.clear()
    result = runner.invoke(app, [*args, "--no-related"])

    assert result.exit_code == 0, result.output
    assert "related (" not in result.output
    assert fake.calls == [("rain", Kind.AUDIO_MUSIC)]


def test_search_cleared_only_drops_copyrighted_and_unknown() -> None:
    _register_two()  # youtube (unknown license -> copyrighted) and stock (CC0 -> clear)
    registry.register(FakeProvider("ia", [_asset("ia", "ia1")]))  # unknown

    result = runner.invoke(app, ["search", "rain", "--no-cache", "--cleared-only"])

    assert result.exit_code == 0, result.output
    assert "stock:st1" in result.output
    assert "youtube:yt1" not in result.output
    assert "ia:ia1" not in result.output

    result = runner.invoke(app, ["search", "rain", "--no-cache"])
    assert "youtube:yt1" in result.output and "ia:ia1" in result.output


def test_recall_command_prints_names() -> None:
    result = runner.invoke(app, ["recall", "cat crying at the dinner table"])
    assert result.exit_code == 0, result.output
    assert "woman yelling at cat" in result.output.splitlines()


def test_recall_command_kind_and_no_suggestions() -> None:
    result = runner.invoke(app, ["recall", "undertale sans", "--kind", "audio_music"])
    assert result.exit_code == 0, result.output
    assert "megalovania" in result.output
    result = runner.invoke(app, ["recall", "rain on window"])
    assert result.exit_code == 0, result.output
    assert "no suggestions" in result.output
    result = runner.invoke(app, ["recall", "x", "--kind", "nope"])
    assert result.exit_code == 1


def test_search_safe_by_default_and_unsafe_flag() -> None:
    registry.register(
        FakeProvider(
            "stock",
            [_asset("stock", "ok1", title="Rain"), _asset("stock", "bad1", title="NSFW rain")],
        )
    )
    safe = runner.invoke(app, ["search", "rain", "--no-expand"])
    assert safe.exit_code == 0, safe.output
    assert "stock:ok1" in safe.output
    assert "stock:bad1" not in safe.output
    assert "1 result(s) hidden by safe search (--unsafe to show)" in safe.output
    unsafe = runner.invoke(app, ["search", "rain", "--no-expand", "--unsafe"])
    assert unsafe.exit_code == 0, unsafe.output
    assert "stock:bad1" in unsafe.output
    assert "hidden by safe search" not in unsafe.output


def test_search_safe_follows_config(monkeypatch: pytest.MonkeyPatch) -> None:
    registry.register(FakeProvider("stock", [_asset("stock", "bad1", title="NSFW rain")]))
    monkeypatch.setenv("CLIPFORAGE_SAFE_SEARCH", "off")
    result = runner.invoke(app, ["search", "rain", "--no-expand"])
    assert result.exit_code == 0, result.output
    assert "stock:bad1" in result.output


def test_search_prints_relaxed_query_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforage import service
    from clipforage.models import SearchOutcome

    def fake_search_expanded(query: str, filters: SearchFilters, **kwargs: object) -> SearchOutcome:
        assert kwargs["cleared_only"] is True
        return SearchOutcome(assets=[], errors=[], relaxed_query="phonk")

    monkeypatch.setattr(service, "search_expanded", fake_search_expanded)
    args = ["search", "phonk background music", "--kind", "audio_music", "--cleared-only"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert 'no cleared results for "phonk background music"; showing "phonk"' in result.output

    monkeypatch.setattr(
        service, "search_expanded", lambda *a, **k: SearchOutcome(assets=[], errors=[])
    )
    result = runner.invoke(app, args)
    assert "no cleared results for" not in result.output
