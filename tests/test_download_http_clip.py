"""ffmpeg clip ranges for plain-HTTP plans. ffmpeg itself is faked (no network, no binary)."""

import io
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from clipforage.download import binaries
from clipforage.download import service as dl
from clipforage.download.service import DownloadError, download_http_clip, download_plan
from clipforage.http import USER_AGENT
from clipforage.models import Asset, DownloadPlan, Kind

MEDIA_URL = "https://archive.org/download/cat_film/cat_film.mp4"
FFMPEG = Path("C:/tools/ffmpeg.exe")


def _plan(ext: str = "mp4", headers: dict[str, str] | None = None) -> DownloadPlan:
    asset = Asset(
        provider="internet_archive",
        id="cat_film",
        kind=Kind.AUDIO_MUSIC if ext == "mp3" else Kind.VIDEO,
        title='Cat: "film"?',
        source_url="https://archive.org/details/cat_film",
        license="Public Domain",
        attribution_text="Cat film via Internet Archive",
        commercial_ok=True,
    )
    return DownloadPlan(asset=asset, method="http", url=MEDIA_URL, ext=ext, headers=headers or {})


class FakePopen:
    """Records argv, writes the output file and replays ``-progress`` lines."""

    calls: ClassVar[list[list[str]]] = []
    returncode_to_use = 0
    progress_lines: ClassVar[list[str]] = []
    stderr_text = ""
    write_output = True

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        FakePopen.calls.append(list(argv))
        self.kwargs = kwargs
        self.stdout = io.StringIO("".join(f"{line}\n" for line in self.progress_lines))
        self.stderr = io.StringIO(self.stderr_text)
        self.returncode: int | None = None
        if self.write_output:
            Path(argv[-1]).write_bytes(b"fake clip")

    def wait(self) -> int:
        self.returncode = self.returncode_to_use
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> type[FakePopen]:
    FakePopen.calls = []
    FakePopen.returncode_to_use = 0
    FakePopen.progress_lines = [
        "frame=1",
        "out_time_us=1000000",
        "out_time_ms=2000000",
        "out_time_us=N/A",
        "out_time_us=3000000",
        "out_time_us=5000000",
        "progress=end",
    ]
    FakePopen.stderr_text = ""
    FakePopen.write_output = True
    monkeypatch.setattr(dl.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(binaries, "find_tool", lambda name: FFMPEG)
    return FakePopen


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_video_clip_argv_file_and_sidecar(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    result = download_http_clip(_plan(), (2.0, 6.5), tmp_path)

    argv = fake_ffmpeg.calls[0]
    assert argv[0] == str(FFMPEG)
    assert {"-hide_banner", "-nostdin", "-y", "-nostats"} <= set(argv)
    assert _value_after(argv, "-progress") == "pipe:1"
    assert _value_after(argv, "-ss") == "2.000"
    assert _value_after(argv, "-to") == "6.500"
    assert _value_after(argv, "-i") == MEDIA_URL
    assert argv.index("-ss") < argv.index("-i")
    assert _value_after(argv, "-user_agent") == USER_AGENT
    assert argv.index("-user_agent") < argv.index("-i")
    assert "-headers" not in argv
    assert _value_after(argv, "-c:v") == "libx264"
    assert _value_after(argv, "-preset") == "veryfast"
    assert _value_after(argv, "-crf") == "20"
    assert _value_after(argv, "-pix_fmt") == "yuv420p"
    assert _value_after(argv, "-c:a") == "aac"
    assert _value_after(argv, "-movflags") == "+faststart"
    assert argv[-1].endswith(".mp4.part")

    assert result.path == tmp_path / "Cat_ _film__ [cat_film] 2-6.5.mp4"
    assert result.path.read_bytes() == b"fake clip"
    assert not list(tmp_path.glob("*.part"))
    meta = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert meta["clip"] == {"start_s": 2.0, "end_s": 6.5}
    assert meta["format"] == "mp4"
    assert meta["license"] == "Public Domain"
    assert meta["provider"] == "internet_archive"


@pytest.mark.parametrize("ext", ["webm", "mov", "mkv"])
def test_other_video_containers_become_mp4(
    tmp_path: Path, fake_ffmpeg: type[FakePopen], ext: str
) -> None:
    result = download_http_clip(_plan(ext), (0, 4), tmp_path)
    assert result.path.name == "Cat_ _film__ [cat_film] 0-4.mp4"
    assert "libx264" in fake_ffmpeg.calls[0]


@pytest.mark.parametrize("ext", ["mp3", "ogg", "wav", "flac", "m4a"])
def test_audio_plan_uses_mp3_flags(tmp_path: Path, fake_ffmpeg: type[FakePopen], ext: str) -> None:
    result = download_http_clip(_plan(ext), (1, 3), tmp_path)

    argv = fake_ffmpeg.calls[0]
    assert "-vn" in argv
    assert _value_after(argv, "-c:a") == "libmp3lame"
    assert _value_after(argv, "-q:a") == "2"
    assert "libx264" not in argv
    assert result.path.name == "Cat_ _film__ [cat_film] 1-3.mp3"
    assert json.loads(result.sidecar.read_text(encoding="utf-8"))["format"] == "mp3"


def test_plan_headers_become_ffmpeg_headers(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    download_http_clip(_plan(headers={"Authorization": "k", "X-A": "b"}), (0, 1), tmp_path)

    argv = fake_ffmpeg.calls[0]
    assert _value_after(argv, "-headers") == "Authorization: k\r\nX-A: b\r\n"
    assert argv.index("-headers") < argv.index("-i")


def test_custom_filename_keeps_range_suffix(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    result = download_http_clip(_plan(), (10, 25), tmp_path, filename="my clip")
    assert result.path.name == "my clip 10-25.mp4"


def test_sub_second_ranges_get_distinct_names(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    first = download_http_clip(_plan(), (12.3, 15.8), tmp_path)
    second = download_http_clip(_plan(), (12.7, 16.1), tmp_path)
    assert first.path.name == "Cat_ _film__ [cat_film] 12.3-15.8.mp4"
    assert second.path.name == "Cat_ _film__ [cat_film] 12.7-16.1.mp4"
    assert first.path.exists() and second.path.exists()


def test_existing_file_gets_numbered_suffix(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    taken = tmp_path / "Cat_ _film__ [cat_film] 0.5-1.5.mp4"
    taken.write_bytes(b"old clip")
    (tmp_path / "Cat_ _film__ [cat_film] 0.5-1.5 (2).mp4").write_bytes(b"old clip 2")

    result = download_http_clip(_plan(), (0.5, 1.5), tmp_path)

    assert result.path == tmp_path / "Cat_ _film__ [cat_film] 0.5-1.5 (3).mp4"
    assert result.sidecar == tmp_path / "Cat_ _film__ [cat_film] 0.5-1.5 (3).mp4.json"
    assert result.path.read_bytes() == b"fake clip"
    assert result.sidecar.exists()
    assert taken.read_bytes() == b"old clip"
    assert fake_ffmpeg.calls[0][-1].endswith("0.5-1.5 (3).mp4.part")


def test_second_identical_clip_gets_suffix_2(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    first = download_http_clip(_plan(), (1, 2), tmp_path)
    second = download_http_clip(_plan(), (1, 2), tmp_path)
    assert first.path.name == "Cat_ _film__ [cat_film] 1-2.mp4"
    assert second.path.name == "Cat_ _film__ [cat_film] 1-2 (2).mp4"
    assert second.sidecar.name == "Cat_ _film__ [cat_film] 1-2 (2).mp4.json"
    assert first.sidecar.exists() and second.sidecar.exists()


@pytest.mark.parametrize(
    ("clip", "label"),
    [
        ((2.0, 6.5), "2-6.5"),
        ((0, 4), "0-4"),
        ((0.0, 1.0), "0-1"),
        ((-0.0, 1.0), "0-1"),
        ((12.345, 15.8), "12.345-15.8"),
        ((10.5, 25.25), "10.5-25.25"),
        ((1.23456, 2.0004), "1.235-2"),
        ((100.0, 3600.1), "100-3600.1"),
    ],
)
def test_clip_label(clip: tuple[float, float], label: str) -> None:
    assert dl.clip_label(*clip) == label
    assert "%" not in dl.clip_label(*clip)


def test_progress_fraction_monotonic_then_finished(
    tmp_path: Path, fake_ffmpeg: type[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dl, "PROGRESS_EVERY_S", 0.0)
    events: list[dict[str, Any]] = []

    result = download_http_clip(_plan(), (2, 6), tmp_path, progress=events.append)

    downloading = [e for e in events if e["status"] == "downloading"]
    fractions = [e["fraction"] for e in downloading]
    assert fractions == [0.25, 0.5, 0.75, 1.0]  # 5 s of a 4 s clip is capped at 1
    assert all(e["downloaded_bytes"] is None and e["total_bytes"] is None for e in downloading)
    assert events[-1] == {"status": "finished", "filename": str(result.path), "fraction": 1.0}


def test_progress_is_throttled(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    events: list[dict[str, Any]] = []
    download_http_clip(_plan(), (2, 6), tmp_path, progress=events.append)
    # All lines arrive at once: only the first update fits in the 0.25 s window.
    assert [e["status"] for e in events] == ["downloading", "finished"]


def test_ffmpeg_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "find_tool", lambda name: None)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ffmpeg must not run")

    monkeypatch.setattr(dl.subprocess, "Popen", boom)
    with pytest.raises(DownloadError, match=r"ffmpeg not found; run `clipforage setup`"):
        download_http_clip(_plan(), (0, 1), tmp_path / "lib")
    assert not (tmp_path / "lib").exists()


def test_nonzero_exit_raises_with_stderr_tail(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    fake_ffmpeg.returncode_to_use = 1
    fake_ffmpeg.stderr_text = "".join(f"line {i}\n" for i in range(30)) + "Server returned 404\n"

    with pytest.raises(DownloadError) as info:
        download_http_clip(_plan(), (0, 1), tmp_path)

    message = str(info.value)
    assert "ffmpeg exited with code 1" in message
    assert "Server returned 404" in message
    assert "line 29" in message
    assert "line 10\n" not in message  # only the last 20 stderr lines
    assert "line 11" in message
    assert list(tmp_path.iterdir()) == []  # .part removed, no sidecar


def test_zero_exit_without_output_raises(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    fake_ffmpeg.write_output = False
    with pytest.raises(DownloadError, match="wrote no file"):
        download_http_clip(_plan(), (0, 1), tmp_path)


def test_rejects_ytdlp_plan(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    plan = _plan().model_copy(update={"method": "ytdlp"})
    with pytest.raises(ValueError, match="needs an http plan"):
        download_http_clip(plan, (0, 1), tmp_path)


def test_download_plan_dispatches_http_clip(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    result = download_plan(_plan(), tmp_path, clip=(3, 5))
    assert result.path.name == "Cat_ _film__ [cat_film] 3-5.mp4"
    assert len(fake_ffmpeg.calls) == 1


def test_download_plan_passes_clip_to_ytdlp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_download_url(url: str, out_dir: Path | None = None, **kwargs: Any) -> Any:
        seen.update(url=url, **kwargs)
        return "result"

    monkeypatch.setattr(dl, "download_url", fake_download_url)
    plan = _plan().model_copy(update={"method": "ytdlp", "url": "https://youtu.be/x"})
    assert download_plan(plan, tmp_path, clip=(1, 2)) == "result"
    assert seen["clip"] == (1, 2)
    assert seen["url"] == "https://youtu.be/x"


def test_http_input_gets_timeout_and_reconnect_flags(
    tmp_path: Path, fake_ffmpeg: type[FakePopen]
) -> None:
    download_http_clip(_plan(), (0, 1), tmp_path)

    argv = fake_ffmpeg.calls[0]
    assert _value_after(argv, "-rw_timeout") == "30000000"
    assert _value_after(argv, "-reconnect") == "1"
    assert _value_after(argv, "-reconnect_streamed") == "1"
    assert _value_after(argv, "-reconnect_delay_max") == "5"
    for flag in ("-rw_timeout", "-reconnect", "-reconnect_streamed", "-reconnect_delay_max"):
        assert argv.index(flag) < argv.index("-i")


def test_non_http_input_has_no_http_flags(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    plan = _plan().model_copy(update={"url": str(tmp_path / "local.mp4")})
    download_http_clip(plan, (0, 1), tmp_path)
    argv = fake_ffmpeg.calls[0]
    assert "-rw_timeout" not in argv
    assert "-reconnect" not in argv


def test_unknown_ext_falls_back_to_video_args(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    result = download_http_clip(_plan("bin"), (0, 2), tmp_path)
    assert "libx264" in fake_ffmpeg.calls[0]
    assert result.path.suffix == ".mp4"


class HangingPopen(FakePopen):
    """An ffmpeg that writes a partial file and never finishes until killed."""

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        super().__init__(argv, **kwargs)
        self.killed = threading.Event()
        self.stdout = self._lines()  # type: ignore[assignment]

    def _lines(self) -> Iterator[str]:
        yield "out_time_us=100000\n"
        self.killed.wait(timeout=10)

    def wait(self) -> int:
        self.killed.wait(timeout=10)
        return -9

    def poll(self) -> int | None:
        return -9 if self.killed.is_set() else None

    def kill(self) -> None:
        self.killed.set()


def test_wall_clock_limit_kills_ffmpeg_and_cleans_up(
    tmp_path: Path, fake_ffmpeg: type[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[HangingPopen] = []

    def make(argv: list[str], **kwargs: Any) -> HangingPopen:
        proc = HangingPopen(argv, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(dl.subprocess, "Popen", make)
    started = time.monotonic()
    with pytest.raises(DownloadError) as info:
        download_http_clip(_plan(), (0, 4), tmp_path, max_wall_s=0.2)

    assert time.monotonic() - started < 5
    assert created[0].killed.is_set()
    message = str(info.value)
    assert message.startswith("clip took longer than 0.2 s; the remote file is probably badly")
    assert "download the full file with `clipforage get <ref>` and trim locally" in message
    assert list(tmp_path.iterdir()) == []  # .part removed, no sidecar


def test_default_wall_clock_limit_is_300_s() -> None:
    assert dl.CLIP_MAX_WALL_S == 300


@pytest.mark.parametrize("max_wall_s", [0, 0.0, -1])
def test_zero_or_negative_wall_clock_limit_starts_no_timer(
    tmp_path: Path,
    fake_ffmpeg: type[FakePopen],
    monkeypatch: pytest.MonkeyPatch,
    max_wall_s: float,
) -> None:
    def no_timer(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no watchdog Timer expected")

    monkeypatch.setattr(dl.threading, "Timer", no_timer)
    result = download_http_clip(_plan(), (0, 1), tmp_path, max_wall_s=max_wall_s)
    assert result.path.is_file()


def test_none_wall_clock_limit_uses_default(
    tmp_path: Path, fake_ffmpeg: type[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    intervals: list[float] = []
    real_timer = threading.Timer

    def record_timer(interval: float, function: Any) -> threading.Timer:
        intervals.append(interval)
        return real_timer(interval, function)

    monkeypatch.setattr(dl.threading, "Timer", record_timer)
    download_http_clip(_plan(), (0, 1), tmp_path, max_wall_s=None)
    assert intervals == [dl.CLIP_MAX_WALL_S]


def test_download_plan_forwards_max_wall_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_clip(plan: DownloadPlan, clip: Any, out_dir: Path | None = None, **kw: Any) -> str:
        seen.append(kw)
        return "result"

    monkeypatch.setattr(dl, "download_http_clip", fake_clip)
    assert download_plan(_plan(), tmp_path, clip=(1, 2), max_wall_s=12.5) == "result"
    assert download_plan(_plan(), tmp_path, clip=(1, 2)) == "result"
    assert seen[0]["max_wall_s"] == 12.5
    assert "max_wall_s" not in seen[1]  # None leaves download_http_clip's default


def test_cli_progress_prints_clipping_percent(capsys: pytest.CaptureFixture[str]) -> None:
    from clipforage import cli

    cli._print_progress(
        {"status": "downloading", "downloaded_bytes": None, "total_bytes": None, "fraction": 0.42}
    )
    assert capsys.readouterr().out == "\rclipping 42%"
    cli._print_progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
    assert "downloading  50.0%" in capsys.readouterr().out


def test_cli_progress_prints_done_once_for_final_event(capsys: pytest.CaptureFixture[str]) -> None:
    from clipforage import cli

    cli._print_progress({"status": "finished", "filename": "x.webm"})  # yt-dlp's own event
    cli._print_progress({"status": "postprocessing", "postprocessor": "FFmpegExtractAudio"})
    cli._print_progress({"status": "finished", "filename": "x.mp3", "fraction": 1.0})  # final
    out = capsys.readouterr().out
    assert out.count("done") == 1
    assert out == "\rprocessing (FFmpegExtractAudio)...\rdownloading done  \n"


def test_job_progress_hook_uses_fraction() -> None:
    from clipforage.web.jobs import Job, JobManager

    manager = JobManager(max_workers=1)
    try:
        job = Job(job_id="j1", ref="internet_archive:x", url=None)
        hook = manager._progress_hook(job)
        hook({"status": "downloading", "downloaded_bytes": None, "fraction": 0.42})
        assert job.progress == 0.42
        assert job.downloaded_bytes is None
        hook({"status": "downloading", "downloaded_bytes": 25, "total_bytes": 100})
        assert job.progress == 0.25
    finally:
        manager.shutdown()
