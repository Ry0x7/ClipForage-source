"""Progress contract: each downloader ends with exactly one ``finished`` event carrying
``fraction`` 1.0, and no other ``finished`` event carries it. Reuses the fakes of the
per-downloader test modules (no network, no ffmpeg, no yt-dlp)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import test_download_http as http_tests
import test_download_http_clip as clip_tests
import test_download_keyout as keyout_tests
import test_download_service as ytdlp_tests

from clipforage.download import binaries
from clipforage.download import service as dl
from clipforage.http import make_client

Events = list[dict[str, Any]]


def _drive_download_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: Events) -> None:
    class NativeEventsYDL(ytdlp_tests.FakeYDL):
        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            home = Path(self.opts["paths"]["home"])
            for hook in self.opts["progress_hooks"]:  # yt-dlp's own per-file events
                hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
                hook({"status": "finished", "filename": str(home / "x.f137.mp4")})
                hook({"status": "finished", "filename": str(home / "x.f140.m4a")})
            for hook in self.opts["postprocessor_hooks"]:
                hook({"status": "started", "postprocessor": "Merger", "info_dict": {}})
                hook({"status": "finished", "postprocessor": "Merger", "info_dict": {}})
            return super().extract_info(url, download)

    info = {"id": "abc123", "title": "Big Buck Bunny", "webpage_url": ytdlp_tests.YT_URL}
    dl.download_url(
        ytdlp_tests.YT_URL,
        tmp_path,
        progress=events.append,
        ydl_factory=lambda opts: NativeEventsYDL(opts, info, "mp4"),
    )


def _drive_download_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: Events) -> None:
    body = b"x" * (600 * 1024)
    with respx.mock:
        respx.get(http_tests.MEDIA_URL).mock(
            return_value=httpx.Response(
                200, content=body, headers={"content-length": str(len(body))}
            )
        )
        with make_client() as client:
            dl.download_http(http_tests._plan(), tmp_path, client=client, progress=events.append)


def _drive_download_http_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: Events
) -> None:
    fake = clip_tests.FakePopen
    monkeypatch.setattr(fake, "calls", [])
    monkeypatch.setattr(fake, "returncode_to_use", 0)
    monkeypatch.setattr(
        fake, "progress_lines", ["out_time_us=1000000", "out_time_us=3000000", "progress=end"]
    )
    monkeypatch.setattr(fake, "stderr_text", "")
    monkeypatch.setattr(fake, "write_output", True)
    monkeypatch.setattr(dl.subprocess, "Popen", fake)
    monkeypatch.setattr(binaries, "find_tool", lambda name: clip_tests.FFMPEG)
    dl.download_http_clip(clip_tests._plan(), (2.0, 6.0), tmp_path, progress=events.append)


def _drive_key_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: Events) -> None:
    fake = keyout_tests.FakePopen
    monkeypatch.setattr(fake, "calls", [])
    monkeypatch.setattr(fake, "returncode_to_use", 0)
    monkeypatch.setattr(
        fake, "progress_lines", ["out_time_us=1000000", "out_time_us=4000000", "progress=end"]
    )
    monkeypatch.setattr(fake, "stderr_text", f"{keyout_tests.DURATION_LINE}\n")
    monkeypatch.setattr(fake, "write_output", True)
    monkeypatch.setattr(dl.subprocess, "Popen", fake)
    monkeypatch.setattr(binaries, "find_tool", lambda name: keyout_tests.FFMPEG)
    dl.key_out(keyout_tests._src(tmp_path), progress=events.append)


@pytest.mark.parametrize(
    "drive",
    [_drive_download_url, _drive_download_http, _drive_download_http_clip, _drive_key_out],
    ids=["download_url", "download_http", "download_http_clip", "key_out"],
)
def test_single_final_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drive: Callable[[Path, pytest.MonkeyPatch, Events], None],
) -> None:
    events: Events = []
    drive(tmp_path, monkeypatch, events)

    final = [
        e for e in events if e.get("status") == "finished" and e.get(dl.FINAL_FRACTION_KEY) == 1.0
    ]
    assert len(final) == 1, events
    assert events[-1] is final[0]
    assert len(events) > 1, events  # intermediate events were emitted too
    assert all(
        dl.FINAL_FRACTION_KEY not in e for e in events[:-1] if e.get("status") == "finished"
    ), events


def test_finished_event_shape() -> None:
    assert dl.finished_event(Path("a") / "b.mp4") == {
        "status": "finished",
        "filename": str(Path("a") / "b.mp4"),
        "fraction": 1.0,
    }
    assert dl.finished_event("x.mp4", downloaded_bytes=3, total_bytes=3) == {
        "status": "finished",
        "filename": "x.mp4",
        "fraction": 1.0,
        "downloaded_bytes": 3,
        "total_bytes": 3,
    }
