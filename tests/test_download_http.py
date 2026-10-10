import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.download.service import download_http, download_plan, safe_filename
from clipforage.http import USER_AGENT, make_client
from clipforage.models import Asset, DownloadPlan, Kind

MEDIA_URL = "https://cdn.example.com/files/clip.mp4"
BODY = b"\x00\x01fake-mp4-bytes" * 1000


def _plan(url: str = MEDIA_URL, ext: str = "mp4") -> DownloadPlan:
    asset = Asset(
        provider="pexels",
        id="12345",
        kind=Kind.VIDEO,
        title='Green: screen "cat"?',
        source_url="https://www.pexels.com/video/12345/",
        license="Pexels License",
        attribution_text="Video by Someone on Pexels",
        commercial_ok=True,
    )
    return DownloadPlan(
        asset=asset, method="http", url=url, ext=ext, headers={"Authorization": "k"}
    )


@respx.mock
def test_download_http_writes_file_and_sidecar(tmp_path: Path) -> None:
    route = respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=BODY))
    with make_client() as client:
        result = download_http(_plan(), tmp_path, client=client)

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "k"
    assert request.headers["User-Agent"] == USER_AGENT

    assert result.path == tmp_path / "Green_ screen _cat__ [12345].mp4"
    assert result.path.suffix == ".mp4"
    assert result.path.read_bytes() == BODY
    assert not list(tmp_path.glob("*.part"))

    assert result.sidecar == tmp_path / (result.path.name + ".json")
    meta = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert meta["license"] == "Pexels License"
    assert meta["attribution_text"] == "Video by Someone on Pexels"
    assert meta["source_url"] == "https://www.pexels.com/video/12345/"
    assert meta["provider"] == "pexels"
    assert meta["fetched_at"] == result.fetched_at.isoformat()
    assert meta["format"] == "mp4"


@respx.mock
def test_download_http_custom_filename_and_dotted_ext(tmp_path: Path) -> None:
    respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"abc"))
    with make_client() as client:
        result = download_http(_plan(ext=".jpg"), tmp_path, client=client, filename="my/pic")
    assert result.path == tmp_path / "my_pic.jpg"
    assert result.path.read_bytes() == b"abc"


@respx.mock
def test_download_http_404_raises_and_leaves_nothing(tmp_path: Path) -> None:
    respx.get(MEDIA_URL).mock(return_value=httpx.Response(404))
    with make_client() as client, pytest.raises(httpx.HTTPStatusError):
        download_http(_plan(), tmp_path, client=client)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_download_http_reports_progress(tmp_path: Path) -> None:
    body = b"x" * (600 * 1024)
    respx.get(MEDIA_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-length": str(len(body))})
    )
    events: list[dict[str, Any]] = []
    with make_client() as client:
        download_http(_plan(), tmp_path, client=client, progress=events.append)

    downloading = [e for e in events if e["status"] == "downloading"]
    assert downloading
    assert all(e["total_bytes"] == len(body) for e in downloading)
    assert all(0 < e["downloaded_bytes"] <= len(body) for e in downloading)
    assert events[-1]["status"] == "finished"
    assert events[-1]["downloaded_bytes"] == len(body)
    assert events[-1]["total_bytes"] == len(body)
    assert [e for e in events if e["status"] == "finished"] == [events[-1]]
    assert downloading[-1]["downloaded_bytes"] == downloading[-1]["total_bytes"] == len(body)
    assert events[-1]["fraction"] == 1.0


@respx.mock
def test_download_plan_passes_progress_to_http(tmp_path: Path) -> None:
    respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"abc"))
    events: list[dict[str, Any]] = []
    with make_client() as client:
        download_plan(_plan(), tmp_path, client=client, progress=events.append)
    assert events == [
        {"status": "downloading", "downloaded_bytes": 3, "total_bytes": 3},
        {
            "status": "finished",
            "downloaded_bytes": 3,
            "total_bytes": 3,
            "filename": str(tmp_path / "Green_ screen _cat__ [12345].mp4"),
            "fraction": 1.0,
        },
    ]


def test_download_http_rejects_ytdlp_plan(tmp_path: Path) -> None:
    plan = _plan().model_copy(update={"method": "ytdlp"})
    with pytest.raises(ValueError, match="http plan"):
        download_http(plan, tmp_path)


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ('a\\b/c:d*e?f"g<h>i|j\x01k', "a_b_c_d_e_f_g_h_i_j_k"),
        ("trailing dots... ", "trailing dots"),
        ("", "download"),
        ("x" * 300, "x" * 120),
    ],
)
def test_safe_filename(stem: str, expected: str) -> None:
    assert safe_filename(stem) == expected
