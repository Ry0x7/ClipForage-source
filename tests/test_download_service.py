import json
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
import yt_dlp.utils

from clipforage.download import service
from clipforage.download.service import (
    DownloadError,
    asset_from_url,
    build_ydl_opts,
    download_url,
    parse_clip,
    parse_cookies_from_browser,
    video_format_for,
)
from clipforage.models import Kind

YT_URL = "https://www.youtube.com/watch?v=abc123"


# --- parse_clip -------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("10-25", (10.0, 25.0)),
        ("1.5-2.5", (1.5, 2.5)),
        ("00:05-00:12", (5.0, 12.0)),
        ("1:05-2:00", (65.0, 120.0)),
        ("01:00:00-01:00:30", (3600.0, 3630.0)),
        (" 0:05 - 0:12 ", (5.0, 12.0)),
    ],
)
def test_parse_clip_valid(spec: str, expected: tuple[float, float]) -> None:
    assert parse_clip(spec) == expected


@pytest.mark.parametrize("spec", ["25-10", "10-10", "00:12-00:05"])
def test_parse_clip_bad_order(spec: str) -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        parse_clip(spec)


@pytest.mark.parametrize("spec", ["", "abc", "10", "10-", "-10", "a-b", "1-2-3", "1:2:3:4-5"])
def test_parse_clip_garbage(spec: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        parse_clip(spec)


# --- parse_cookies_from_browser ---------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("firefox", ("firefox", None, None, None)),
        ("Chrome", ("chrome", None, None, None)),
        ("chrome:Default", ("chrome", "Default", None, None)),
        ("Firefox+gnomekeyring:default::Meta", ("firefox", "default", "GNOMEKEYRING", "Meta")),
        ("firefox::Personal", ("firefox", None, None, "Personal")),
    ],
)
def test_parse_cookies_from_browser_valid(
    spec: str, expected: tuple[str, str | None, str | None, str | None]
) -> None:
    assert parse_cookies_from_browser(spec) == expected


def test_parse_cookies_from_browser_bad_browser() -> None:
    with pytest.raises(ValueError, match="unsupported browser") as excinfo:
        parse_cookies_from_browser("netscape")
    assert "firefox" in str(excinfo.value)


def test_parse_cookies_from_browser_bad_keyring() -> None:
    with pytest.raises(ValueError, match="unsupported keyring"):
        parse_cookies_from_browser("chrome+nope")


def test_parse_cookies_from_browser_garbage() -> None:
    with pytest.raises(ValueError, match="Supported browsers"):
        parse_cookies_from_browser("")


# --- build_ydl_opts ---------------------------------------------------------


def test_opts_mp4(tmp_path: Path) -> None:
    opts = build_ydl_opts(tmp_path, audio_only=False, clip=None, ffmpeg=None, progress=None)
    assert opts["format"] == (
        "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
    )
    assert opts["merge_output_format"] == "mp4"
    assert "postprocessors" not in opts
    assert "download_ranges" not in opts
    assert "ffmpeg_location" not in opts
    assert "progress_hooks" not in opts
    assert "postprocessor_hooks" not in opts
    assert opts["paths"] == {"home": str(tmp_path)}
    assert opts["outtmpl"] == {"default": "%(title).120B [%(id)s].%(ext)s"}
    assert opts["windowsfilenames"] is True
    assert opts["noplaylist"] is True
    assert opts["quiet"] is True
    assert opts["no_warnings"] is True
    assert opts["noprogress"] is True


def test_opts_silent_logger(tmp_path: Path) -> None:
    opts = build_ydl_opts(tmp_path, audio_only=False, clip=None, ffmpeg=None, progress=None)
    logger = opts["logger"]
    for name in ("debug", "info", "warning", "error"):
        assert callable(getattr(logger, name))
    logger.error("ERROR: first")
    logger.error("ERROR: last")
    assert logger.errors == ["ERROR: first", "ERROR: last"]


def test_opts_mp3(tmp_path: Path) -> None:
    opts = build_ydl_opts(tmp_path, audio_only=True, clip=None, ffmpeg="ffmpeg", progress=None)
    assert opts["format"] == "bestaudio/best"
    assert "merge_output_format" not in opts
    assert opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
    ]


def test_opts_clip(tmp_path: Path) -> None:
    opts = build_ydl_opts(tmp_path, audio_only=False, clip=(10.0, 25.0), ffmpeg=None, progress=None)
    ranges = opts["download_ranges"]
    assert isinstance(ranges, yt_dlp.utils.download_range_func)
    assert ranges.ranges == [(10.0, 25.0)]
    assert ranges.chapters is None
    assert opts["force_keyframes_at_cuts"] is False
    assert opts["outtmpl"]["default"] == "%(title).120B [%(id)s] 10-25.%(ext)s"


def test_opts_clip_keeps_sub_second_label(tmp_path: Path) -> None:
    opts = build_ydl_opts(
        tmp_path, audio_only=False, clip=(10.5, 25.25), ffmpeg=None, progress=None
    )
    assert opts["outtmpl"]["default"] == "%(title).120B [%(id)s] 10.5-25.25.%(ext)s"
    assert "section_start" not in opts["outtmpl"]["default"]


def test_opts_ffmpeg_and_progress(tmp_path: Path) -> None:
    def hook(d: dict[str, Any]) -> None:
        pass

    opts = build_ydl_opts(
        tmp_path, audio_only=False, clip=None, ffmpeg=r"C:\tools\ffmpeg.exe", progress=hook
    )
    assert opts["ffmpeg_location"] == r"C:\tools\ffmpeg.exe"
    assert opts["progress_hooks"] == [hook]
    assert len(opts["postprocessor_hooks"]) == 1


def test_opts_postprocessor_hook_maps_started_only(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    opts = build_ydl_opts(
        tmp_path, audio_only=True, clip=None, ffmpeg="ffmpeg", progress=events.append
    )
    pp_hook = opts["postprocessor_hooks"][0]
    pp_hook({"status": "started", "postprocessor": "ExtractAudio", "info_dict": {}})
    pp_hook({"status": "processing", "postprocessor": "ExtractAudio", "info_dict": {}})
    pp_hook({"status": "finished", "postprocessor": "ExtractAudio", "info_dict": {}})
    assert events == [
        {"status": "postprocessing", "postprocessor": "ExtractAudio", "fraction": None}
    ]


def test_opts_postprocessor_hook_reports_started_once_per_name(tmp_path: Path) -> None:
    # yt-dlp attaches the hook twice to FFmpegExtractAudio, so ``started`` arrives twice.
    events: list[dict[str, Any]] = []
    opts = build_ydl_opts(
        tmp_path, audio_only=True, clip=None, ffmpeg="ffmpeg", progress=events.append
    )
    pp_hook = opts["postprocessor_hooks"][0]
    pp_hook({"status": "started", "postprocessor": "ExtractAudio", "info_dict": {}})
    pp_hook({"status": "started", "postprocessor": "ExtractAudio", "info_dict": {}})
    pp_hook({"status": "started", "postprocessor": "Merger", "info_dict": {}})
    assert events == [
        {"status": "postprocessing", "postprocessor": "ExtractAudio", "fraction": None},
        {"status": "postprocessing", "postprocessor": "Merger", "fraction": None},
    ]


def test_opts_cookies_from_browser(tmp_path: Path) -> None:
    cookies = ("firefox", None, None, None)
    opts = build_ydl_opts(
        tmp_path,
        audio_only=False,
        clip=None,
        ffmpeg=None,
        progress=None,
        cookies_from_browser=cookies,
    )
    assert opts["cookiesfrombrowser"] == cookies
    plain = build_ydl_opts(tmp_path, audio_only=False, clip=None, ffmpeg=None, progress=None)
    assert "cookiesfrombrowser" not in plain


# --- download_url with a fake yt-dlp ----------------------------------------


class FakeYDL:
    def __init__(self, opts: dict[str, Any], info: dict[str, Any], ext: str) -> None:
        self.opts = opts
        self.info = info
        self.ext = ext

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        home = Path(self.opts["paths"]["home"])
        file = home / f"{self.info['title']} [{self.info['id']}].{self.ext}"
        file.write_bytes(b"fake media")
        return {**self.info, "requested_downloads": [{"filepath": str(file)}]}


def _factory(
    recorded: list[dict[str, Any]], info: dict[str, Any] | None = None, ext: str = "mp4"
) -> Any:
    info = info or {
        "id": "abc123",
        "title": "Big Buck Bunny",
        "uploader": "Blender Foundation",
        "webpage_url": YT_URL,
        "extractor_key": "Youtube",
        "duration": 33,
    }

    def make(opts: dict[str, Any]) -> FakeYDL:
        recorded.append(opts)
        return FakeYDL(opts, info, ext)

    return make


def test_download_url_reports_postprocessing_and_final_file(tmp_path: Path) -> None:
    class MergingYDL(FakeYDL):
        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            home = Path(self.opts["paths"]["home"])
            for hook in self.opts["progress_hooks"]:
                hook({"status": "finished", "filename": str(home / "x.f137.mp4")})
            for hook in self.opts["postprocessor_hooks"]:
                # Twice, as yt-dlp does for user-supplied postprocessors; reported once.
                for _ in range(2):
                    hook({"status": "started", "postprocessor": "Merger", "info_dict": {}})
                    hook({"status": "finished", "postprocessor": "Merger", "info_dict": {}})
            return super().extract_info(url, download)

    events: list[dict[str, Any]] = []
    info = {"id": "abc123", "title": "Big Buck Bunny", "webpage_url": YT_URL}

    result = download_url(
        YT_URL,
        tmp_path,
        progress=events.append,
        ydl_factory=lambda opts: MergingYDL(opts, info, "mp4"),
    )

    assert [e["status"] for e in events] == ["finished", "postprocessing", "finished"]
    assert events[0]["filename"].endswith(".f137.mp4")  # yt-dlp's own event passes through
    assert events[1]["postprocessor"] == "Merger"
    assert events[-1] == {"status": "finished", "filename": str(result.path), "fraction": 1.0}


def test_download_url_youtube_mp4(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []
    out = tmp_path / "lib" / "nested"

    result = download_url(YT_URL, out, ydl_factory=_factory(recorded))

    assert out.is_dir()
    assert result.path.exists()
    assert result.path.parent == out
    assert result.sidecar == result.path.with_name(result.path.name + ".json")
    assert result.sidecar.exists()
    data = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert data["provider"] == "youtube"
    assert data["license"] == "unknown"
    assert data["kind"] == "video"
    assert data["format"] == "mp4"
    assert "clip" not in data
    assert "Blender Foundation" in data["attribution_text"]
    assert recorded[0]["paths"]["home"] == str(out)


def test_download_url_non_youtube_provider(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []
    info = {
        "id": "bbb",
        "title": "Clip",
        "uploader": "Someone",
        "webpage_url": "https://vimeo.com/1",
        "extractor_key": "Vimeo",
    }

    result = download_url("https://vimeo.com/1", tmp_path, ydl_factory=_factory(recorded, info))

    data = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert data["provider"] == "vimeo"
    assert "via Vimeo" in data["attribution_text"]


def test_download_url_clip_and_mp3_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Path("C:/ffmpeg/ffmpeg.exe")
    monkeypatch.setattr(service.binaries, "find_tool", lambda name: fake)
    recorded: list[dict[str, Any]] = []

    result = download_url(
        YT_URL,
        tmp_path,
        audio_only=True,
        clip=(5.0, 12.0),
        ydl_factory=_factory(recorded, ext="mp3"),
    )

    data = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert data["format"] == "mp3"
    assert data["kind"] == "audio_music"
    assert data["clip"] == {"start_s": 5.0, "end_s": 12.0}
    assert recorded[0]["ffmpeg_location"] == str(fake)


def test_download_url_wraps_bot_check(tmp_path: Path) -> None:
    def boom(opts: dict[str, Any]) -> Any:
        raise yt_dlp.utils.DownloadError("Sign in to confirm you're not a bot")

    with pytest.raises(DownloadError) as excinfo:
        download_url(YT_URL, tmp_path, ydl_factory=boom)

    message = str(excinfo.value)
    assert "Sign in to confirm" in message
    assert "sign-in" in message


def test_download_url_login_hint(tmp_path: Path) -> None:
    def boom(opts: dict[str, Any]) -> Any:
        raise yt_dlp.utils.DownloadError(
            "ERROR: [vimeo] 123: This video is only available for logged-in users"
        )

    with pytest.raises(DownloadError) as excinfo:
        download_url("https://vimeo.com/123", tmp_path, ydl_factory=boom)

    message = str(excinfo.value)
    assert "requires a login" in message
    assert "sign-in" not in message


def test_bot_hint_does_not_match_unrelated_bot_word(tmp_path: Path) -> None:
    def boom(opts: dict[str, Any]) -> Any:
        raise yt_dlp.utils.DownloadError("ERROR: Unsupported URL: https://robotics.example.com")

    with pytest.raises(DownloadError) as excinfo:
        download_url("https://robotics.example.com", tmp_path, ydl_factory=boom)

    assert "sign-in" not in str(excinfo.value)
    assert "update yt-dlp" in str(excinfo.value)


def test_download_url_empty_error_uses_logged_message(tmp_path: Path) -> None:
    def make(opts: dict[str, Any]) -> Any:
        opts["logger"].error("ERROR: [generic] real reason")
        raise yt_dlp.utils.DownloadError("")

    with pytest.raises(DownloadError, match="real reason"):
        download_url(YT_URL, tmp_path, ydl_factory=make)


def test_download_url_generic_error_hint(tmp_path: Path) -> None:
    def boom(opts: dict[str, Any]) -> Any:
        raise yt_dlp.utils.DownloadError("ERROR: Unsupported URL: https://example.com")

    with pytest.raises(DownloadError, match="update yt-dlp"):
        download_url("https://example.com", tmp_path, ydl_factory=boom)


def test_download_url_missing_filepath(tmp_path: Path) -> None:
    class NoFile(FakeYDL):
        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            return {"id": "x", "requested_downloads": []}

    with pytest.raises(DownloadError, match="no output file"):
        download_url(YT_URL, tmp_path, ydl_factory=lambda opts: NoFile(opts, {}, "mp4"))


@pytest.mark.parametrize(
    "kwargs", [{"clip": (1.0, 2.0)}, {"audio_only": True}], ids=["clip", "mp3"]
)
def test_download_url_needs_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    monkeypatch.setattr(service.binaries, "find_tool", lambda name: None)
    recorded: list[dict[str, Any]] = []

    with pytest.raises(DownloadError, match="ffmpeg"):
        download_url(YT_URL, tmp_path, ydl_factory=_factory(recorded), **kwargs)

    assert recorded == []


def test_download_url_explicit_cookies(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []

    download_url(
        YT_URL, tmp_path, ydl_factory=_factory(recorded), cookies_from_browser="chrome:Default"
    )

    assert recorded[0]["cookiesfrombrowser"] == ("chrome", "Default", None, None)


def test_download_url_cookies_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_COOKIES_FROM_BROWSER", "edge")
    recorded: list[dict[str, Any]] = []

    download_url(YT_URL, tmp_path, ydl_factory=_factory(recorded))

    assert recorded[0]["cookiesfrombrowser"] == ("edge", None, None, None)


def test_download_url_no_cookies_by_default(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []

    download_url(YT_URL, tmp_path, ydl_factory=_factory(recorded))

    assert "cookiesfrombrowser" not in recorded[0]


def test_download_url_invalid_cookies(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []

    with pytest.raises(DownloadError, match="unsupported browser"):
        download_url(
            YT_URL, tmp_path, ydl_factory=_factory(recorded), cookies_from_browser="netscape"
        )

    assert recorded == []


def test_download_url_uses_setup_deno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deno = service.binaries.bin_dir() / service.binaries.exe("deno")
    deno.parent.mkdir(parents=True)
    deno.write_bytes(b"")
    monkeypatch.setattr(
        service.binaries, "find_tool", lambda name: deno if name == "deno" else None
    )
    recorded: list[dict[str, Any]] = []

    download_url(YT_URL, tmp_path / "out", ydl_factory=_factory(recorded))

    assert recorded[0]["js_runtimes"] == {"deno": {"path": str(deno)}}


def test_download_url_path_deno_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deno = tmp_path / "elsewhere" / service.binaries.exe("deno")
    monkeypatch.setattr(
        service.binaries, "find_tool", lambda name: deno if name == "deno" else None
    )
    recorded: list[dict[str, Any]] = []

    download_url(YT_URL, tmp_path / "out", ydl_factory=_factory(recorded))

    assert "js_runtimes" not in recorded[0]


# --- asset_from_url ---------------------------------------------------------


def test_asset_from_url_fallback_is_deterministic() -> None:
    url = "https://example.com/video.mp4"

    first = asset_from_url(url)
    second = asset_from_url(url)

    assert first.id == second.id
    assert len(first.id) == 11
    assert first.provider == "url"
    assert first.title == url
    assert first.source_url == url
    assert first.license == "unknown"
    assert first.commercial_ok is None
    assert "by unknown via example.com" in first.attribution_text
    assert asset_from_url("https://example.com/other.mp4").id != first.id


def test_asset_from_url_youtu_be_and_kind() -> None:
    asset = asset_from_url("https://youtu.be/abc123", {"id": "abc123"}, kind=Kind.AUDIO_MUSIC)
    assert asset.provider == "youtube"
    assert asset.kind is Kind.AUDIO_MUSIC


# --- quality ----------------------------------------------------------------

CAPPED_720 = (
    "bv*[ext=mp4][vcodec^=avc1][height<=720]+ba[ext=m4a]/bv*[ext=mp4][height<=720]+ba[ext=m4a]"
    "/b[ext=mp4][height<=720]/b[height<=720]/b"
)


@pytest.mark.parametrize("quality", ["best", None])
def test_video_format_for_best_is_unchanged(quality: str | None) -> None:
    assert video_format_for(quality) == service.MP4_FORMAT


def test_video_format_for_height_cap() -> None:
    assert video_format_for("720") == CAPPED_720


def test_opts_quality_caps_mp4(tmp_path: Path) -> None:
    opts = build_ydl_opts(
        tmp_path, audio_only=False, clip=None, ffmpeg=None, progress=None, quality="720"
    )
    assert opts["format"] == CAPPED_720
    assert opts["merge_output_format"] == "mp4"


def test_opts_quality_ignored_for_mp3(tmp_path: Path) -> None:
    opts = build_ydl_opts(
        tmp_path, audio_only=True, clip=None, ffmpeg="ffmpeg", progress=None, quality="720"
    )
    assert opts["format"] == "bestaudio/best"
    assert opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
    ]


@pytest.mark.parametrize("quality", ["999", "720p", "", "BEST"])
def test_invalid_quality_raises(tmp_path: Path, quality: str) -> None:
    with pytest.raises(DownloadError, match="hint: use one of best, 2160, 1440"):
        video_format_for(quality)
    with pytest.raises(DownloadError, match="unknown quality"):
        build_ydl_opts(
            tmp_path, audio_only=False, clip=None, ffmpeg=None, progress=None, quality=quality
        )


def test_download_url_quality_in_opts_and_sidecar(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []

    result = download_url(YT_URL, tmp_path, ydl_factory=_factory(recorded), quality="720")

    assert recorded[0]["format"] == CAPPED_720
    data = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert data["quality"] == "720"


def test_download_url_default_quality_sidecar(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []

    result = download_url(YT_URL, tmp_path, ydl_factory=_factory(recorded))

    assert recorded[0]["format"] == service.MP4_FORMAT
    data = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert data["quality"] == "best"


def test_download_url_invalid_quality_never_runs_ydl(tmp_path: Path) -> None:
    recorded: list[dict[str, Any]] = []

    with pytest.raises(DownloadError, match="unknown quality '999'"):
        download_url(YT_URL, tmp_path, ydl_factory=_factory(recorded), quality="999")

    assert recorded == []
