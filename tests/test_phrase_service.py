import json
from pathlib import Path
from typing import Any, Self

import httpx
import pytest
import respx
import yt_dlp

from clipforage import service
from clipforage.cache import Cache
from clipforage.models import Asset, Kind
from clipforage.phrase import captions
from clipforage.providers.internet_archive import SEARCH_URL

FIXTURES = Path(__file__).parent / "fixtures"
VTT = (FIXTURES / "phrase" / "youtube_auto.vtt").read_text(encoding="utf-8")
SRT = (FIXTURES / "phrase" / "film.srt").read_text(encoding="utf-8")
IA_ID = "fake_pd_horror_1968"
IA_METADATA_URL = f"https://archive.org/metadata/{IA_ID}"
IA_SRT_URL = f"https://archive.org/download/{IA_ID}/{IA_ID}.en.srt"


def vtt_url(video_id: str) -> str:
    return f"https://www.youtube.com/api/timedtext?v={video_id}&fmt=vtt"


def flat_entry(video_id: str, duration: float = 120) -> dict[str, Any]:
    return {
        "_type": "url",
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"Video {video_id}",
        "duration": duration,
        "channel": "Fake Channel",
    }


def video_info(video_id: str, manual: bool = False) -> dict[str, Any]:
    track = {"ext": "vtt", "url": vtt_url(video_id), "name": "English"}
    return {
        "id": video_id,
        "requested_subtitles": {"en": track},
        "subtitles": {"en": [track]} if manual else {},
        "automatic_captions": {"en": [track], "en-orig": [track]},
    }


class FakeYDL:
    def __init__(self, factory: "FakeFactory", opts: dict[str, Any]) -> None:
        self.factory = factory
        self.opts = opts

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> Any:
        assert download is False
        self.factory.calls.append((url, self.opts))
        if self.factory.fail_search and "extract_flat" in self.opts:
            raise yt_dlp.utils.DownloadError("ERROR: search exploded")
        if "extract_flat" in self.opts:
            if url in self.factory.flat:
                return self.factory.flat[url]
            return {"entries": [flat_entry(v) for v in self.factory.search_ids]}
        video_id = url.rsplit("=", 1)[-1]
        if video_id in self.factory.broken:
            raise yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot")
        if video_id in self.factory.no_subs:
            return {"id": video_id, "requested_subtitles": None}
        return video_info(video_id)


class FakeFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.search_ids: list[str] = ["vid1", "vid2"]
        self.flat: dict[str, Any] = {}
        self.broken: set[str] = set()
        self.no_subs: set[str] = set()
        self.fail_search = False

    def __call__(self, opts: dict[str, Any]) -> FakeYDL:
        return FakeYDL(self, opts)

    def caption_calls(self) -> list[str]:
        return [url for url, opts in self.calls if opts.get("writesubtitles")]


@pytest.fixture
def ydl(monkeypatch: pytest.MonkeyPatch) -> FakeFactory:
    factory = FakeFactory()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", factory)
    return factory


def mock_youtube_vtt() -> None:
    respx.get(url__startswith="https://www.youtube.com/api/timedtext").mock(
        return_value=httpx.Response(200, text=VTT)
    )


def mock_ia(search_status: int = 200) -> None:
    search = json.loads(
        (FIXTURES / "internet_archive" / "search_subtitles.json").read_text(encoding="utf-8")
    )
    metadata = json.loads(
        (FIXTURES / "internet_archive" / "metadata_subtitles.json").read_text(encoding="utf-8")
    )
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(search_status, json=search))
    respx.get(IA_METADATA_URL).mock(return_value=httpx.Response(200, json=metadata))
    respx.get(IA_SRT_URL).mock(return_value=httpx.Response(200, text=SRT))


@respx.mock
def test_youtube_phrase_hits_and_auto_flag(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    outcome = service.phrase_search("best coffee", sources=["youtube"])
    hits, errors = outcome.hits, outcome.errors
    assert errors == []
    assert [(h.ref, h.start_s, h.end_s) for h in hits] == [
        ("youtube:vid1", 12.84, 14.639),
        ("youtube:vid2", 12.84, 14.639),
    ]
    hit = hits[0]
    assert hit.text == "today we're making the best coffee"
    assert hit.lang == "en"
    assert hit.auto_generated is True
    assert hit.asset.title == "Video vid1"
    assert hit.clip_spec() == "11.8-15.6"
    search_url = ydl.calls[0][0]
    assert search_url.startswith("ytsearch") and search_url.endswith(":best coffee")
    caption_opts = dict(ydl.calls[1][1])
    assert caption_opts["writeautomaticsub"] is True
    assert caption_opts["subtitleslangs"] == ["en", "en-orig", "en.*"]
    assert caption_opts["subtitlesformat"] == "vtt"
    assert caption_opts["skip_download"] is True


@respx.mock
def test_hit_refs_resolve_for_get(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    hits = service.phrase_search("best coffee", sources=["youtube"]).hits
    provider, asset = service.resolve_ref(hits[0].ref)
    assert provider.id == "youtube"
    assert asset == hits[0].asset


@respx.mock
def test_captions_cached_between_runs(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    service.phrase_search("best coffee", sources=["youtube"])
    assert len(ydl.caption_calls()) == 2
    hits = service.phrase_search("not a drill", sources=["youtube"]).hits
    assert len(ydl.caption_calls()) == 2  # nothing refetched
    assert [h.text for h in hits] == ["this is not a drill", "this is not a drill"]
    service.phrase_search("not a drill", sources=["youtube"], use_cache=False)
    assert len(ydl.caption_calls()) == 4


@respx.mock
def test_previously_indexed_videos_are_searched(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    service.phrase_search("best coffee", sources=["youtube"])
    ydl.search_ids = ["vid3"]
    hits = service.phrase_search("welcome back", sources=["youtube"]).hits
    assert sorted(h.asset.id for h in hits) == ["vid1", "vid2", "vid3"]


@respx.mock
def test_video_without_captions_is_stored_and_skipped(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    ydl.no_subs = {"vid2"}
    hits = service.phrase_search("best coffee", sources=["youtube"]).hits
    assert [h.asset.id for h in hits] == ["vid1"]
    with Cache() as cache:
        assert cache.has_captions("youtube", "vid2") is True


@respx.mock
def test_failing_caption_fetch_warns_and_continues(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    ydl.broken = {"vid1"}
    warnings: list[str] = []
    outcome = service.phrase_search("best coffee", sources=["youtube"], warn=warnings.append)
    hits, errors = outcome.hits, outcome.errors
    assert errors == []
    assert [h.asset.id for h in hits] == ["vid2"]
    assert len(warnings) == 1
    assert warnings[0].startswith("youtube:vid1: could not fetch captions: ")
    assert "Sign in" in warnings[0]
    with Cache() as cache:
        assert cache.has_captions("youtube", "vid1") is False  # retried next time


@respx.mock
def test_internet_archive_phrase_hit() -> None:
    mock_ia()
    outcome = service.phrase_search("coming to get you", sources=["internet_archive"])
    hits, errors = outcome.hits, outcome.errors
    assert errors == []
    assert len(hits) == 1
    hit = hits[0]
    assert hit.ref == f"internet_archive:{IA_ID}"
    assert (hit.start_s, hit.end_s) == (5.0, 7.5)
    assert hit.text == "They're coming to get you, Barbara."
    assert hit.lang == "en"
    assert hit.auto_generated is False
    assert hit.asset.license == "Public Domain Mark"
    q = respx.calls[0].request.url.params["q"]
    assert q == (
        '(coming to get you) AND mediatype:movies AND (format:"SubRip" '
        'OR format:"Web Video Text Tracks")'
    )
    assert respx.calls[0].request.url.params["rows"] == "10"


@respx.mock
def test_one_source_failing_does_not_fail_the_other(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    mock_ia(search_status=503)
    outcome = service.phrase_search("best coffee")
    hits, errors = outcome.hits, outcome.errors
    assert [h.asset.provider for h in hits] == ["youtube", "youtube"]
    assert [e.provider for e in errors] == ["internet_archive"]
    assert "HTTP 503" in str(errors[0])


@respx.mock
def test_youtube_search_failure_is_a_provider_error(ydl: FakeFactory) -> None:
    mock_ia()
    ydl.fail_search = True
    outcome = service.phrase_search("coming to get you")
    hits, errors = outcome.hits, outcome.errors
    assert [h.asset.provider for h in hits] == ["internet_archive"]
    assert [e.provider for e in errors] == ["youtube"]


@respx.mock
def test_within_playlist_url(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    playlist = "https://www.youtube.com/playlist?list=PLfake"
    ydl.flat[playlist] = {
        "_type": "playlist",
        "entries": [flat_entry("pl1", 4000), {"id": "live1", "live_status": "is_live"}, None],
    }
    outcome = service.phrase_search(
        "best coffee", sources=["youtube"], within=playlist, max_videos=3
    )
    hits, errors = outcome.hits, outcome.errors
    assert errors == []
    assert [h.asset.id for h in hits] == ["pl1"]
    url, opts = ydl.calls[0]
    assert url == playlist
    assert opts["extract_flat"] == "in_playlist"
    assert opts["playlistend"] == 3
    assert hits[0].asset.duration_s == 4000.0  # no duration cap for --within


@respx.mock
def test_within_single_video_url(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    video = "https://www.youtube.com/watch?v=single1"
    ydl.flat[video] = flat_entry("single1")
    hits = service.phrase_search("best coffee", sources=["youtube"], within=video).hits
    assert [h.asset.id for h in hits] == ["single1"]


@respx.mock
def test_within_non_url_warns_and_searches(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    warnings: list[str] = []
    hits = service.phrase_search(
        "best coffee", sources=["youtube"], within="my playlist", warn=warnings.append
    ).hits
    assert len(hits) == 2
    assert ydl.calls[0][0].startswith("ytsearch")
    assert warnings == ["--within 'my playlist' is not a URL; searching YouTube instead"]


def test_unknown_source_raises_lookup_error() -> None:
    with pytest.raises(LookupError, match="unknown phrase source 'pexels'"):
        service.phrase_search("x", sources=["pexels"])
    assert service.PHRASE_SOURCES == ("youtube", "internet_archive")


@respx.mock
def test_limit_applies(ydl: FakeFactory) -> None:
    mock_youtube_vtt()
    hits = service.phrase_search("best coffee", sources=["youtube"], limit=1).hits
    assert len(hits) == 1


@respx.mock
def test_fetch_youtube_captions_manual_track_and_injection() -> None:
    respx.get(url__startswith="https://www.youtube.com/api/timedtext").mock(
        return_value=httpx.Response(200, text=VTT)
    )
    calls: list[dict[str, Any]] = []

    class OneShot:
        def __init__(self, opts: dict[str, Any]) -> None:
            calls.append(opts)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = True) -> Any:
            return video_info("m1", manual=True)

    asset = Asset(
        provider="youtube",
        id="m1",
        kind=Kind.VIDEO,
        title="t",
        source_url="https://www.youtube.com/watch?v=m1",
        license="unknown",
        attribution_text="a",
        commercial_ok=None,
    )
    result = captions.fetch_youtube_captions(
        asset, ydl_factory=OneShot, client=httpx.Client(), cookies_from_browser="firefox"
    )
    assert result is not None
    cues, lang, auto = result
    assert lang == "en"
    assert auto is False
    assert cues[1].text == "good morning everyone welcome back"
    assert calls[0]["cookiesfrombrowser"] == ("firefox", None, None, None)


@respx.mock
def test_fetch_ia_captions_prefers_english_and_handles_cp1252() -> None:
    metadata = {
        "files": [
            {"name": "film.mp4", "format": "MPEG4"},
            {"name": "film.es.srt", "format": "SubRip"},
            {"name": "film.english.vtt", "format": "Web Video Text Tracks"},
        ]
    }
    respx.get("https://archive.org/metadata/film").mock(
        return_value=httpx.Response(200, json=metadata)
    )
    body = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nCaf\xe9 society\n".encode("cp1252")
    respx.get("https://archive.org/download/film/film.english.vtt").mock(
        return_value=httpx.Response(200, content=body)
    )
    asset = Asset(
        provider="internet_archive",
        id="film",
        kind=Kind.VIDEO,
        title="Film",
        source_url="https://archive.org/details/film",
        license="unknown",
        attribution_text="a",
        commercial_ok=None,
    )
    result = captions.fetch_ia_captions(asset, client=httpx.Client())
    assert result is not None
    cues, lang, auto = result
    assert [c.text for c in cues] == ["Caf\xe9 society"]
    assert (lang, auto) == ("en", False)


@respx.mock
def test_fetch_ia_captions_none_without_subtitle_files() -> None:
    respx.get("https://archive.org/metadata/film").mock(
        return_value=httpx.Response(200, json={"files": [{"name": "film.mp4"}]})
    )
    asset = Asset(
        provider="internet_archive",
        id="film",
        kind=Kind.VIDEO,
        title="Film",
        source_url="https://archive.org/details/film",
        license="unknown",
        attribution_text="a",
        commercial_ok=None,
    )
    assert captions.fetch_ia_captions(asset, client=httpx.Client()) is None


SUBS_CLAUSE = '(format:"SubRip" OR format:"Web Video Text Tracks")'


def mock_ia_by_query(found_for: set[str]) -> list[str]:
    """IA search answering with the fixture only for queries in ``found_for``; returns qs."""
    search = json.loads(
        (FIXTURES / "internet_archive" / "search_subtitles.json").read_text(encoding="utf-8")
    )
    metadata = json.loads(
        (FIXTURES / "internet_archive" / "metadata_subtitles.json").read_text(encoding="utf-8")
    )
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        seen.append(q)
        if q in found_for:
            return httpx.Response(200, json=search)
        return httpx.Response(200, json={"response": {"docs": []}})

    respx.get(SEARCH_URL).mock(side_effect=answer)
    respx.get(IA_METADATA_URL).mock(return_value=httpx.Response(200, json=metadata))
    respx.get(IA_SRT_URL).mock(return_value=httpx.Response(200, text=SRT))
    return seen


@respx.mock
def test_ia_within_identifier_is_queried_first() -> None:
    q_id = f"(identifier:{IA_ID}) AND mediatype:movies AND {SUBS_CLAUSE}"
    seen = mock_ia_by_query({q_id})
    outcome = service.phrase_search("coming to get you", sources=["internet_archive"], within=IA_ID)
    hits, errors = outcome.hits, outcome.errors
    assert errors == []
    assert [h.ref for h in hits] == [f"internet_archive:{IA_ID}"]
    assert seen == [q_id]


@respx.mock
def test_ia_within_identifier_miss_falls_back_to_title_search() -> None:
    q_title = f"(fake_title) AND mediatype:movies AND {SUBS_CLAUSE}"
    seen = mock_ia_by_query({q_title})
    hits = service.phrase_search(
        "coming to get you", sources=["internet_archive"], within="fake_title"
    ).hits
    assert len(hits) == 1
    assert seen == [f"(identifier:fake_title) AND mediatype:movies AND {SUBS_CLAUSE}", q_title]


@respx.mock
def test_ia_within_title_with_spaces_is_a_title_search() -> None:
    q_title = f"(Night of the Living Dead) AND mediatype:movies AND {SUBS_CLAUSE}"
    seen = mock_ia_by_query({q_title})
    hits = service.phrase_search(
        "coming to get you", sources=["internet_archive"], within="Night of the Living Dead"
    ).hits
    assert len(hits) == 1
    assert seen == [q_title]


@respx.mock
def test_ia_no_match_falls_back_to_popular_feature_films() -> None:
    q_popular = f"mediatype:movies AND collection:feature_films AND {SUBS_CLAUSE}"
    seen = mock_ia_by_query({q_popular})
    outcome = service.phrase_search("coming to get you", sources=["internet_archive"])
    hits, errors = outcome.hits, outcome.errors
    assert errors == []
    assert len(hits) == 1
    assert seen == [f"(coming to get you) AND mediatype:movies AND {SUBS_CLAUSE}", q_popular]
    assert respx.calls[1].request.url.params["sort[]"] == "downloads desc"


@respx.mock
def test_ia_within_does_not_fall_back_to_popular() -> None:
    seen = mock_ia_by_query(set())
    hits = service.phrase_search("x", sources=["internet_archive"], within="Some Title").hits
    assert hits == []
    assert len(seen) == 1


@respx.mock
def test_ia_asr_subtitles_are_auto_generated() -> None:
    respx.get("https://archive.org/metadata/film").mock(
        return_value=httpx.Response(
            200, json={"files": [{"name": "film.asr.srt", "format": "SubRip"}]}
        )
    )
    respx.get("https://archive.org/download/film/film.asr.srt").mock(
        return_value=httpx.Response(200, text=SRT)
    )
    asset = Asset(
        provider="internet_archive",
        id="film",
        kind=Kind.VIDEO,
        title="Film",
        source_url="https://archive.org/details/film",
        license="unknown",
        attribution_text="a",
        commercial_ok=None,
    )
    result = captions.fetch_ia_captions(asset, client=httpx.Client())
    assert result is not None
    _, lang, auto = result
    assert (lang, auto) == (None, True)


@respx.mock
def test_max_per_video(ydl: FakeFactory) -> None:
    repeated = "WEBVTT\n\n" + "".join(
        f"00:00:{i:02d}.000 --> 00:00:{i:02d}.500\nsay hello {i}\n\n" for i in range(0, 50, 5)
    )
    respx.get(url__startswith="https://www.youtube.com/api/timedtext").mock(
        return_value=httpx.Response(200, text=repeated)
    )
    hits = service.phrase_search("say hello", sources=["youtube"]).hits
    assert sorted(h.asset.id for h in hits) == ["vid1"] * 3 + ["vid2"] * 3
    hits = service.phrase_search("say hello", sources=["youtube"], max_per_video=0).hits
    assert len(hits) == 20
    hits = service.phrase_search("say hello", sources=["youtube"], max_per_video=1, limit=1).hits
    assert len(hits) == 1


@respx.mock
def test_safe_skips_explicit_candidates_and_indexed_hits(
    ydl: FakeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_youtube_vtt()
    plain_entry = flat_entry

    def entry(video_id: str, duration: float = 120) -> dict[str, Any]:
        data = plain_entry(video_id, duration)
        if video_id == "vid2":
            data["title"] = "NSFW compilation"
        return data

    monkeypatch.setitem(globals(), "flat_entry", entry)
    outcome = service.phrase_search("best coffee", sources=["youtube"])
    assert [h.asset.id for h in outcome.hits] == ["vid1"]
    assert outcome.filtered_count == 1  # vid2 skipped as a candidate
    assert len(ydl.caption_calls()) == 1  # the explicit candidate was never fetched
    outcome = service.phrase_search("best coffee", sources=["youtube"], safe=False)
    assert sorted(h.asset.id for h in outcome.hits) == ["vid1", "vid2"]
    assert outcome.filtered_count == 0
    # vid2 is in the caption index now; a safe search still hides it.
    outcome = service.phrase_search("best coffee", sources=["youtube"])
    assert [h.asset.id for h in outcome.hits] == ["vid1"]
    # vid2 dropped twice (candidate and indexed hit) counts once.
    assert outcome.filtered_count == 1
    # Not a candidate this time: only the indexed-hit drop counts it.
    ydl.search_ids = ["vid1"]
    outcome = service.phrase_search("best coffee", sources=["youtube"])
    assert [h.asset.id for h in outcome.hits] == ["vid1"]
    assert outcome.filtered_count == 1
