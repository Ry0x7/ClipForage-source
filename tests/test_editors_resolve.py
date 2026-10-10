"""Resolve media pool import against a fake Resolve object graph (no Resolve needed)."""

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from clipforage.editors import resolve as rv


class FakeClip:
    def __init__(self, path: str) -> None:
        self.path = path
        self.metadata: dict[str, str] = {}

    def GetClipProperty(self, name: str | None = None) -> Any:
        props = {"File Path": self.path, "Clip Name": Path(self.path).name}
        return props if name is None else props[name]

    def SetMetadata(self, metadata: dict[str, str]) -> bool:
        self.metadata.update(metadata)
        return True


class FakeFolder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.subfolders: list[FakeFolder] = []
        self.clips: list[FakeClip] = []

    def GetName(self) -> str:
        return self.name

    def GetSubFolderList(self) -> list["FakeFolder"]:
        return list(self.subfolders)

    def GetClipList(self) -> list[FakeClip]:
        return list(self.clips)


class FakeMediaPool:
    def __init__(self, reject: set[str] | None = None, accepts: str = "str") -> None:
        self.accepts = accepts
        self.root = FakeFolder("Master")
        self.current = self.root
        self.reject = reject or set()
        self.import_calls: list[list[dict[str, str]]] = []
        self.add_calls = 0

    def GetRootFolder(self) -> FakeFolder:
        return self.root

    def AddSubFolder(self, parent: FakeFolder, name: str) -> FakeFolder:
        self.add_calls += 1
        folder = FakeFolder(name)
        parent.subfolders.append(folder)
        return folder

    def SetCurrentFolder(self, folder: FakeFolder) -> bool:
        self.current = folder
        return True

    def ImportMedia(self, infos: list[Any]) -> list[FakeClip] | None:
        """Like Resolve 21.1 (observed): plain paths work, the dict form returns None."""
        self.import_calls.append(infos)
        if self.accepts == "str":
            if not all(isinstance(i, str) for i in infos):
                return None
            paths = infos
        else:
            if not all(isinstance(i, dict) for i in infos):
                return None
            paths = [i["FilePath"] for i in infos]
        clips = [FakeClip(p) for p in paths if Path(p).name not in self.reject]
        self.current.clips.extend(clips)
        return clips


class FakeResolve:
    def __init__(self, pool: FakeMediaPool | None, has_project: bool = True) -> None:
        self.pool = pool
        self.has_project = has_project

    def GetProjectManager(self) -> Any:
        project = types.SimpleNamespace(GetMediaPool=lambda: self.pool)
        current = project if self.has_project else None
        return types.SimpleNamespace(GetCurrentProject=lambda: current)


def _sidecar(media: Path, **data: Any) -> None:
    media.with_name(media.name + ".json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("a.mp4", "b.mp3", "c.webm.part", ".hidden.mp4"):
        (lib / name).write_bytes(b"x")
    _sidecar(
        lib / "a.mp4",
        license="CC0",
        attribution_text="Clip by Someone",
        source_url="https://example.com/a",
    )
    sub = lib / "sub"
    sub.mkdir()
    (sub / "d.mp4").write_bytes(b"x")
    return lib


def test_library_media_files_excludes_sidecars_part_and_hidden(library: Path) -> None:
    assert [p.name for p in rv.library_media_files(library)] == ["a.mp4", "b.mp3"]
    names = [p.name for p in rv.library_media_files(library, recursive=True)]
    assert names == ["a.mp4", "b.mp3", "d.mp4"]


def test_library_media_files_missing_dir(tmp_path: Path) -> None:
    assert rv.library_media_files(tmp_path / "nope") == []


def test_import_creates_bin_once_and_is_idempotent(library: Path) -> None:
    pool = FakeMediaPool()
    resolve = FakeResolve(pool)

    first = rv.import_library(resolve, library)
    assert [Path(p).name for p in first.imported] == ["a.mp4", "b.mp3"]
    assert first.skipped_existing == [] and first.failed == []
    assert first.bin_name == "clipforage"
    assert pool.add_calls == 1
    assert pool.current.GetName() == "clipforage"
    assert len(pool.import_calls) == 1  # one ImportMedia call for all files
    assert pool.import_calls[0] == [str(library / n) for n in ("a.mp4", "b.mp3")]

    second = rv.import_library(resolve, library)
    assert second.imported == [] and second.failed == []
    assert [Path(p).name for p in second.skipped_existing] == ["a.mp4", "b.mp3"]
    assert pool.add_calls == 1
    assert len(pool.import_calls) == 1  # nothing new -> ImportMedia not called
    assert [f.GetName() for f in pool.root.subfolders] == ["clipforage"]


def test_import_falls_back_to_clip_info_dicts(library: Path) -> None:
    pool = FakeMediaPool(accepts="dict")
    report = rv.import_library(FakeResolve(pool), library)
    assert [Path(p).name for p in report.imported] == ["a.mp4", "b.mp3"]
    assert report.failed == []
    assert pool.import_calls[1] == [{"FilePath": str(library / n)} for n in ("a.mp4", "b.mp3")]


def test_all_failed_when_import_returns_none(library: Path) -> None:
    pool = FakeMediaPool(reject={"a.mp4", "b.mp3"})
    report = rv.import_library(FakeResolve(pool), library)
    assert report.imported == []
    assert [Path(p).name for p in report.failed] == ["a.mp4", "b.mp3"]


def test_import_all_reimports_existing(library: Path) -> None:
    pool = FakeMediaPool()
    rv.import_library(FakeResolve(pool), library)
    again = rv.import_library(FakeResolve(pool), library, only_new=False)
    assert [Path(p).name for p in again.imported] == ["a.mp4", "b.mp3"]
    assert again.skipped_existing == []


def test_import_uses_existing_bin_with_custom_name(library: Path) -> None:
    pool = FakeMediaPool()
    existing = pool.AddSubFolder(pool.root, "memes")
    report = rv.import_library(FakeResolve(pool), library, bin_name="memes")
    assert pool.add_calls == 1
    assert report.bin_name == "memes"
    assert [Path(c.path).name for c in existing.clips] == ["a.mp4", "b.mp3"]


def test_failed_files_are_paths_not_returned(library: Path) -> None:
    pool = FakeMediaPool(reject={"b.mp3"})
    report = rv.import_library(FakeResolve(pool), library)
    assert [Path(p).name for p in report.imported] == ["a.mp4"]
    assert [Path(p).name for p in report.failed] == ["b.mp3"]


def test_sidecar_license_written_to_comments(library: Path) -> None:
    pool = FakeMediaPool()
    rv.import_library(FakeResolve(pool), library)
    clips = {Path(c.path).name: c for c in pool.current.clips}
    assert clips["a.mp4"].metadata == {"Comments": "CC0 | Clip by Someone | https://example.com/a"}
    assert clips["b.mp3"].metadata == {}  # no sidecar -> no metadata


def test_metadata_falls_back_to_description_and_never_fails(library: Path) -> None:
    class PickyClip(FakeClip):
        def SetMetadata(self, metadata: dict[str, str]) -> bool:
            if "Comments" in metadata:
                return False
            return super().SetMetadata(metadata)

    class BrokenClip(FakeClip):
        def SetMetadata(self, metadata: dict[str, str]) -> bool:
            raise RuntimeError("boom")

    for cls, expected in ((PickyClip, ["Description"]), (BrokenClip, [])):
        pool = FakeMediaPool()
        made: list[FakeClip] = []

        def fake_import(
            infos: list[str], cls: type = cls, made: list[FakeClip] = made
        ) -> list[FakeClip]:
            made.extend(cls(i) for i in infos)
            return list(made)

        pool.ImportMedia = fake_import  # type: ignore[method-assign]
        report = rv.import_library(FakeResolve(pool), library)
        assert [Path(p).name for p in report.imported] == ["a.mp4", "b.mp3"]
        assert report.failed == []
        assert list(made[0].metadata) == expected


def test_import_without_project_raises(library: Path) -> None:
    with pytest.raises(rv.ResolveNotAvailable, match="no project"):
        rv.import_library(FakeResolve(FakeMediaPool(), has_project=False), library)


@pytest.fixture(autouse=True)
def _isolate_resolve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() may set RESOLVE_SCRIPT_* and extend sys.path; undo that after each test."""
    monkeypatch.setenv("RESOLVE_SCRIPT_API", "")
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", "")
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_connect_raises_when_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", None)  # import -> ImportError
    with pytest.raises(rv.ResolveNotAvailable, match="could not load"):
        rv.connect()


def test_connect_raises_when_resolve_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("DaVinciResolveScript")
    fake.scriptapp = lambda name: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", fake)
    with pytest.raises(rv.ResolveNotAvailable, match="External scripting"):
        rv.connect()


def test_connect_returns_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    fake = types.ModuleType("DaVinciResolveScript")
    fake.scriptapp = lambda name: sentinel if name == "Resolve" else None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", fake)
    assert rv.connect() is sentinel
