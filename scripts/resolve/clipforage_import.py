"""clipforage: import the clipforage library folder into the DaVinci Resolve media pool.

Standalone script for Resolve's Workspace > Scripts menu. It does not need clipforage
installed: it reads ``[library] dir`` from clipforage's config.toml if present, else uses
``~/Downloads/clipforage``, and imports new media files into a "clipforage" bin of the
current project, writing "<license> | <attribution> | <source_url>" from each file's
``<file>.json`` sidecar into the clip's Comments metadata.

Install: copy this file into a ``Utility`` subfolder of Resolve's Scripts folder, then
restart Resolve and run it from Workspace > Scripts > clipforage_import.

- Windows (current user): %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility
- Windows (all users):    %PROGRAMDATA%\\Blackmagic Design\\DaVinci Resolve\\Fusion\\Scripts\\Utility
- macOS (current user):   ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility
- macOS (all users):      /Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility
- Linux (current user):   ~/.local/share/DaVinciResolve/Fusion/Scripts/Utility
- Linux (all users):      /opt/resolve/Fusion/Scripts/Utility

The import can also be run outside Resolve with ``clipforage resolve-import``.

Pre-rename installs are still found: the ``memefinder`` config dir and
``~/Downloads/memefinder`` are used when the ``clipforage`` ones do not exist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BIN_NAME = "clipforage"
LEGACY_NAME = "memefinder"  # pre-rename config dir / library folder name
SKIP_SUFFIXES = {".json", ".part", ".ytdl", ".tmp"}


def _config_dir(name: str) -> Path:
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        return Path(appdata) / name
    return Path.home() / ".config" / name


def config_path() -> Path:
    """clipforage's config.toml (mirrors clipforage.config.config_path), else the legacy one."""
    new = _config_dir(BIN_NAME)
    old = _config_dir(LEGACY_NAME)
    if not new.exists() and old.is_dir():
        return old / "config.toml"
    return new / "config.toml"


def _default_library_dir() -> Path:
    """~/Downloads/clipforage, or ~/Downloads/memefinder if only the legacy folder exists."""
    downloads = Path.home() / "Downloads"
    new, old = downloads / BIN_NAME, downloads / LEGACY_NAME
    if not new.exists() and old.is_dir():
        return old
    return new


def library_dir() -> Path:
    """``[library] dir`` from config.toml, else the default folder (as clipforage does)."""
    path = config_path()
    if path.is_file():
        try:
            import tomllib

            with path.open("rb") as fh:
                value = tomllib.load(fh).get("library", {}).get("dir")
            if isinstance(value, str) and value:
                return Path(value)
        except Exception as exc:  # noqa: BLE001 - no tomllib, malformed file, ...
            print(f"clipforage: could not read {path}: {exc}")
    return _default_library_dir()


def media_files(library: Path) -> list[Path]:
    if not library.is_dir():
        return []
    return sorted(
        p
        for p in library.iterdir()
        if p.is_file() and p.suffix.lower() not in SKIP_SUFFIXES and not p.name.startswith(".")
    )


def norm(path: Any) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def clip_path(clip: Any) -> str | None:
    try:
        props = clip.GetClipProperty()
        value = props.get("File Path") if isinstance(props, dict) else None
    except Exception:  # noqa: BLE001 - opaque Resolve API errors mean "no path"
        return None
    return value or None


def license_comment(media: Path) -> str | None:
    try:
        data = json.loads(media.with_name(media.name + ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return " | ".join(str(data.get(k) or "") for k in ("license", "attribution_text", "source_url"))


def get_resolve() -> Any:
    app = globals().get("resolve")
    if app is not None and hasattr(app, "GetProjectManager"):
        return app
    import DaVinciResolveScript as dvr_script  # type: ignore[import-not-found]

    return dvr_script.scriptapp("Resolve")


def main() -> int:
    resolve_app = get_resolve()
    if resolve_app is None:
        print("clipforage: could not connect to DaVinci Resolve")
        return 1
    project = resolve_app.GetProjectManager().GetCurrentProject()
    if not project:
        print("clipforage: no project is open")
        return 1
    library = library_dir()
    files = media_files(library)
    print(f"clipforage: library {library} ({len(files)} media files)")

    pool = project.GetMediaPool()
    root = pool.GetRootFolder()
    folder = next((f for f in root.GetSubFolderList() or [] if f.GetName() == BIN_NAME), None)
    folder = folder or pool.AddSubFolder(root, BIN_NAME)
    if not folder:
        print(f"clipforage: could not create bin {BIN_NAME!r}")
        return 1
    pool.SetCurrentFolder(folder)

    existing = {norm(p) for p in map(clip_path, folder.GetClipList() or []) if p}
    new = [p for p in files if norm(p) not in existing]
    by_norm = {norm(p): p for p in new}
    imported: set[str] = set()
    if new:
        items = pool.ImportMedia([str(p) for p in new])  # dict form returned None on 21.1
        items = items or pool.ImportMedia([{"FilePath": str(p)} for p in new])
        for item in items or []:
            path = clip_path(item)
            if path is None or norm(path) not in by_norm:
                continue
            imported.add(norm(path))
            comment = license_comment(by_norm[norm(path)])
            for key in ("Comments", "Description"):
                try:
                    if comment and item.SetMetadata({key: comment}):
                        break
                except Exception:  # noqa: BLE001, S112 - metadata never fails the import
                    continue
    failed = [p for p in new if norm(p) not in imported]
    print(
        f"clipforage: imported {len(imported)}, skipped {len(files) - len(new)} already in bin, "
        f"failed {len(failed)}"
    )
    for path in failed:
        print(f"  failed: {path.name}")
    return 0


if __name__ == "__main__" or "resolve" in globals():
    main()
