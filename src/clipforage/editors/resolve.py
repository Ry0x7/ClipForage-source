"""Import the clipforage library folder into the DaVinci Resolve media pool.

Uses Resolve's scripting API (``DaVinciResolveScript``). The module is importable without
Resolve installed: the API is only loaded inside :func:`connect`.
"""

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from clipforage.download.sidecar import sidecar_path
from clipforage.models import ImportReport

SKIP_SUFFIXES: frozenset[str] = frozenset({".json", ".part", ".ytdl", ".tmp"})
METADATA_KEYS: tuple[str, ...] = ("Comments", "Description")

NOT_LOADABLE = (
    "could not load the DaVinci Resolve scripting module ({error}). Is DaVinci Resolve "
    "installed? If it is installed in a non-default folder, set RESOLVE_SCRIPT_LIB to its "
    "fusionscript library (e.g. <install dir>\\fusionscript.dll) and RESOLVE_SCRIPT_API to "
    "its Developer\\Scripting folder."
)
NOT_CONNECTED = (
    "could not connect to DaVinci Resolve. Make sure Resolve is running and fully loaded, "
    "and that Preferences > System > General > 'External scripting using' is set to Local."
)


class ResolveNotAvailable(RuntimeError):
    """Resolve cannot be reached: not installed, not running, or external scripting is off."""


def _default_locations() -> tuple[Path, Path] | None:
    """Return the documented default ``(RESOLVE_SCRIPT_API, RESOLVE_SCRIPT_LIB)`` paths."""
    if sys.platform.startswith("win"):
        program_data = Path(os.environ.get("PROGRAMDATA") or "C:\\ProgramData")
        program_files = Path(os.environ.get("PROGRAMFILES") or "C:\\Program Files")
        api = program_data / "Blackmagic Design" / "DaVinci Resolve" / "Support"
        lib = program_files / "Blackmagic Design" / "DaVinci Resolve" / "fusionscript.dll"
        return api / "Developer" / "Scripting", lib
    if sys.platform == "darwin":
        api = Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve")
        app = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")
        lib = app / "Contents" / "Libraries" / "Fusion" / "fusionscript.so"
        return api / "Developer" / "Scripting", lib
    if sys.platform.startswith("linux"):
        root = Path("/opt/resolve")
        return root / "Developer" / "Scripting", root / "libs" / "Fusion" / "fusionscript.so"
    return None


def _prepare_environment() -> None:
    """Set RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB defaults and put ``Modules`` on sys.path."""
    defaults = _default_locations()
    if defaults is not None:
        api, lib = defaults
        if not os.environ.get("RESOLVE_SCRIPT_API") and api.is_dir():
            os.environ["RESOLVE_SCRIPT_API"] = str(api)
        if not os.environ.get("RESOLVE_SCRIPT_LIB") and lib.is_file():
            os.environ["RESOLVE_SCRIPT_LIB"] = str(lib)
    api_env = os.environ.get("RESOLVE_SCRIPT_API")
    if api_env:
        modules = Path(api_env) / "Modules"
        if modules.is_dir() and str(modules) not in sys.path:
            sys.path.append(str(modules))


def connect() -> Any:
    """Return the running Resolve scripting object; raise :class:`ResolveNotAvailable`."""
    _prepare_environment()
    try:
        import DaVinciResolveScript  # type: ignore[import-not-found]
    except Exception as exc:  # ImportError, OSError from the native loader, ...
        raise ResolveNotAvailable(NOT_LOADABLE.format(error=exc)) from exc
    try:
        resolve = DaVinciResolveScript.scriptapp("Resolve")
    except Exception as exc:
        raise ResolveNotAvailable(f"{NOT_CONNECTED} ({exc})") from exc
    if resolve is None:
        raise ResolveNotAvailable(NOT_CONNECTED)
    return resolve


def _is_hidden(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    attrs = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0))


def library_media_files(library: Path, recursive: bool = False) -> list[Path]:
    """Return media files in ``library`` (no sidecars, partial downloads or hidden files)."""
    if not library.is_dir():
        return []
    candidates = library.rglob("*") if recursive else library.iterdir()
    files: list[Path] = []
    for path in candidates:
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        rel_parts = path.relative_to(library).parts
        if any(part.startswith(".") for part in rel_parts) or _is_hidden(path):
            continue
        files.append(path)
    return sorted(files)


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _clip_file_path(clip: Any) -> str | None:
    """Return a media pool item's ``File Path`` clip property, or None."""
    try:
        props = clip.GetClipProperty()
        if isinstance(props, dict):
            value = props.get("File Path")
        else:
            value = clip.GetClipProperty("File Path")
    except Exception:  # noqa: BLE001 - opaque Resolve API errors mean "no path"
        return None
    return value if isinstance(value, str) and value else None


def _find_or_create_bin(media_pool: Any, name: str) -> Any:
    root = media_pool.GetRootFolder()
    for folder in root.GetSubFolderList() or []:
        if folder.GetName() == name:
            return folder
    folder = media_pool.AddSubFolder(root, name)
    if not folder:
        raise RuntimeError(f"could not create media pool bin {name!r}")
    return folder


def _import_media(media_pool: Any, paths: list[Path]) -> list[Any]:
    """Call ``ImportMedia`` once with plain paths; retry with clip-info dicts if that is empty.

    Resolve 21.1 documents ``[{"FilePath": p}]`` as canonical and plain paths as deprecated,
    but observed on 21.1.0.14: plain paths import, the dict form returns None.
    """
    items = media_pool.ImportMedia([str(path) for path in paths])
    if not items:
        items = media_pool.ImportMedia([{"FilePath": str(path)} for path in paths])
    return list(items or [])


def license_comment(media_path: Path) -> str | None:
    """Return ``"<license> | <attribution_text> | <source_url>"`` from the sidecar, if any."""
    sidecar = sidecar_path(media_path)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    parts = [str(data.get(key) or "") for key in ("license", "attribution_text", "source_url")]
    return " | ".join(parts)


def _apply_metadata(clip: Any, comment: str) -> None:
    """Write ``comment`` to the clip's Comments (else Description) metadata; never raises."""
    for key in METADATA_KEYS:
        try:
            if clip.SetMetadata({key: comment}):
                return
        except Exception:  # noqa: BLE001, S112 - metadata must never fail the import
            continue


def import_library(
    resolve: Any,
    library: Path,
    bin_name: str = "clipforage",
    only_new: bool = True,
    recursive: bool = False,
) -> ImportReport:
    """Import the library's media files into a ``bin_name`` bin of the current project."""
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        raise ResolveNotAvailable("no project is open in DaVinci Resolve; open or create one")
    media_pool = project.GetMediaPool()
    folder = _find_or_create_bin(media_pool, bin_name)
    media_pool.SetCurrentFolder(folder)

    report = ImportReport(bin_name=bin_name)
    files = library_media_files(library, recursive=recursive)
    to_import: list[Path] = files
    if only_new:
        existing = {_norm(p) for p in map(_clip_file_path, folder.GetClipList() or []) if p}
        to_import = []
        for path in files:
            if _norm(path) in existing:
                report.skipped_existing.append(str(path))
            else:
                to_import.append(path)
    if not to_import:
        return report

    by_norm = {_norm(path): path for path in to_import}
    items = _import_media(media_pool, to_import)
    imported: set[str] = set()
    for item in items:
        clip_path = _clip_file_path(item)
        if clip_path is None or _norm(clip_path) not in by_norm:
            continue
        key = _norm(clip_path)
        if key in imported:
            continue
        imported.add(key)
        media_path = by_norm[key]
        report.imported.append(str(media_path))
        comment = license_comment(media_path)
        if comment:
            _apply_metadata(item, comment)
    report.failed = [str(path) for path in to_import if _norm(path) not in imported]
    return report
