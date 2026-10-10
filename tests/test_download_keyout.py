"""Chroma-key export (``key_out``). ffmpeg itself is faked (no binary, no network)."""

import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from clipforage.download import binaries
from clipforage.download import service as dl
from clipforage.download.service import DownloadError, chroma_filter, key_out

FFMPEG = Path("C:/tools/ffmpeg.exe")
DURATION_LINE = "  Duration: 00:00:04.00, start: 0.000000, bitrate: 512 kb/s"


class _SlowLines:
    """stdout stand-in that yields lines after a short pause, so the stderr reader thread has
    already seen the ``Duration`` line (real ffmpeg prints it before any progress)."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self) -> Iterator[str]:
        time.sleep(0.05)
        yield from (f"{line}\n" for line in self._lines)


class FakePopen:
    """Records argv, writes the output file and replays ``-progress`` / stderr lines."""

    calls: ClassVar[list[list[str]]] = []
    returncode_to_use = 0
    progress_lines: ClassVar[list[str]] = []
    stderr_text = ""
    write_output = True

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        FakePopen.calls.append(list(argv))
        self.kwargs = kwargs
        self.stdout = _SlowLines(self.progress_lines)
        self.stderr = io.StringIO(self.stderr_text)
        self.returncode: int | None = None
        if self.write_output:
            Path(argv[-1]).write_bytes(b"fake webm")

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
        "out_time_us=N/A",
        "out_time_us=2000000",
        "out_time_us=4000000",
        "progress=end",
    ]
    FakePopen.stderr_text = f"Input #0, mov,mp4, from 'green.mp4':\n{DURATION_LINE}\n"
    FakePopen.write_output = True
    monkeypatch.setattr(dl.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(binaries, "find_tool", lambda name: FFMPEG)
    return FakePopen


def _src(tmp_path: Path, name: str = "green clip.mp4") -> Path:
    src = tmp_path / name
    src.write_bytes(b"mp4")
    return src


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_argv_and_output_name(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    src = _src(tmp_path)
    out = key_out(src)

    argv = fake_ffmpeg.calls[0]
    assert argv[0] == str(FFMPEG)
    assert {"-hide_banner", "-nostdin", "-y", "-nostats"} <= set(argv)
    assert _value_after(argv, "-progress") == "pipe:1"
    assert _value_after(argv, "-i") == str(src)
    assert _value_after(argv, "-vf").startswith("chromakey=color=0x00FF00")
    assert _value_after(argv, "-vf") == chroma_filter(80)
    assert _value_after(argv, "-c:v") == "libvpx-vp9"
    assert _value_after(argv, "-pix_fmt") == "yuva420p"
    assert _value_after(argv, "-auto-alt-ref") == "0"
    assert _value_after(argv, "-c:a") == "libopus"
    assert _value_after(argv, "-f") == "webm"
    assert argv[-1] == str(tmp_path / "green clip.key.webm.part")

    assert out == tmp_path / "green clip.key.webm"
    assert out.read_bytes() == b"fake webm"
    assert not list(tmp_path.glob("*.part"))


def test_tolerance_reaches_filter(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    key_out(_src(tmp_path), tolerance=0)
    assert "similarity=0.05:" in _value_after(fake_ffmpeg.calls[0], "-vf")


def test_existing_output_gets_numbered_suffix(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    src = _src(tmp_path)
    (tmp_path / "green clip.key.webm").write_bytes(b"old")
    out = key_out(src)
    assert out == tmp_path / "green clip (2).key.webm"
    assert (tmp_path / "green clip.key.webm").read_bytes() == b"old"
    out.write_bytes(b"second")
    assert key_out(src) == tmp_path / "green clip (3).key.webm"


def test_progress_fraction_from_duration(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    events: list[dict[str, Any]] = []
    out = key_out(_src(tmp_path), progress=events.append)

    downloading = [e for e in events if e["status"] == "downloading"]
    assert downloading, events
    assert downloading[0] == {
        "status": "downloading",
        "downloaded_bytes": None,
        "total_bytes": None,
        "fraction": 0.25,
    }
    assert events[-1] == {"status": "finished", "filename": str(out), "fraction": 1.0}


def test_no_fraction_without_duration(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    fake_ffmpeg.stderr_text = "Input #0, mov,mp4, from 'green.mp4':\n"
    events: list[dict[str, Any]] = []
    out = key_out(_src(tmp_path), progress=events.append)
    assert events == [{"status": "finished", "filename": str(out), "fraction": 1.0}]


def test_ffmpeg_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "find_tool", lambda name: None)
    with pytest.raises(DownloadError, match="ffmpeg not found; run `clipforage setup`"):
        key_out(_src(tmp_path))


def test_nonzero_exit_raises_with_stderr_tail(tmp_path: Path, fake_ffmpeg: type[FakePopen]) -> None:
    fake_ffmpeg.returncode_to_use = 1
    fake_ffmpeg.stderr_text = "Invalid data found when processing input\n"
    with pytest.raises(DownloadError) as excinfo:
        key_out(_src(tmp_path))
    assert "ffmpeg exited with code 1" in str(excinfo.value)
    assert "Invalid data found when processing input" in str(excinfo.value)
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / "green clip.key.webm").exists()


def test_wall_clock_limit_kills_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed = threading.Event()

    class HangingPopen(FakePopen):
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            super().__init__(argv, **kwargs)
            self.stdout = self._lines()

        def _lines(self) -> Iterator[str]:
            killed.wait(5)
            return
            yield

        def kill(self) -> None:
            self.returncode = -9
            killed.set()

        def wait(self) -> int:
            return -9 if killed.is_set() else 0

    FakePopen.calls = []
    FakePopen.progress_lines = []
    FakePopen.stderr_text = ""
    FakePopen.write_output = True
    monkeypatch.setattr(dl.subprocess, "Popen", HangingPopen)
    monkeypatch.setattr(binaries, "find_tool", lambda name: FFMPEG)
    with pytest.raises(DownloadError, match="keying took longer than 0.05 s"):
        key_out(_src(tmp_path), max_wall_s=0.05)
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    ("tolerance", "similarity"),
    [(0, "0.05"), (80, "0.283"), (120, "0.4"), (-10, "0.05"), (500, "0.4")],
)
def test_chroma_filter_mapping(tolerance: int, similarity: str) -> None:
    assert chroma_filter(tolerance) == (
        f"chromakey=color=0x00FF00:similarity={similarity}:blend=0.1,despill=type=green"
    )
