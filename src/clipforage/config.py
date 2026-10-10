"""User configuration: API keys from env vars or config.toml (read and write)."""

import os
import shutil
import tomllib
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument

APP_NAME = "clipforage"
LEGACY_APP_NAME = "memefinder"  # pre-rename name: legacy dirs and env vars are still read
ENV_PREFIX = "CLIPFORAGE_"
LEGACY_ENV_PREFIX = "MEMEFINDER_"


def _env(name_suffix: str) -> str | None:
    """Return ``CLIPFORAGE_<suffix>`` if set and non-empty, else ``MEMEFINDER_<suffix>``."""
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        value = os.environ.get(prefix + name_suffix)
        if value:
            return value
    return None


def _config_dir(name: str) -> Path:
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        return Path(appdata) / name
    return Path.home() / ".config" / name


def _data_dir(name: str) -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / name
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return Path(xdg) / name
    return Path.home() / ".local" / "share" / name


def config_path() -> Path:
    """Return the path of the user config file (it may not exist)."""
    return _config_dir(APP_NAME) / "config.toml"


def data_dir() -> Path:
    """Return the user data dir for cache.db and fetched binaries (not created here)."""
    return _data_dir(APP_NAME)


def _copy_tree(old: Path, new: Path) -> list[str]:
    """Copy ``old`` into ``new``; a file that cannot be copied is skipped with a warning."""
    skipped: list[str] = []

    def copy_file(src: str, dst: str) -> str:
        try:
            return shutil.copy2(src, dst)
        except OSError as exc:
            skipped.append(f"warning: could not copy {src} ({exc}); skipped")
            return dst

    shutil.copytree(old, new, copy_function=copy_file, dirs_exist_ok=True)
    return skipped


def _migrate_dir(old: Path, new: Path, label: str, copy_name: str | None) -> list[str]:
    if new.exists() or not old.is_dir():
        return []
    try:
        old.rename(new)
    except OSError as exc:
        if copy_name and (old / copy_name).is_file():
            try:
                new.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old / copy_name, new / copy_name)
            except OSError as copy_exc:
                return [f"warning: could not migrate {label} {old} -> {new}: {copy_exc}"]
            return [
                (
                    f"warning: could not move {label} {old} -> {new} ({exc}); "
                    f"copied {copy_name} only, the old folder was left in place"
                )
            ]
        if copy_name is None:  # data dir: copy everything that can be copied, keep the old one
            try:
                skipped = _copy_tree(old, new)
            except (OSError, shutil.Error) as copy_exc:
                return [f"warning: could not migrate {label} {old} -> {new}: {copy_exc}"]
            return [
                *skipped,
                (
                    f"warning: could not move {label} {old} -> {new} ({exc}); "
                    f"copied {label} {old} -> {new} (legacy folder kept)"
                ),
            ]
        return [f"warning: could not move {label} {old} -> {new} ({exc}); old folder kept"]
    return [f"memefinder was renamed to clipforage: moved {label} {old} -> {new}"]


def migrate_legacy_dirs() -> list[str]:
    """Move pre-rename ``memefinder`` config/data dirs to their ``clipforage`` names, once.

    Only acts when the new dir does not exist and the old sibling does (cheap and idempotent).
    The data dir holds cache.db and bin/. If a move fails (cross-device, permissions, a file in
    use), only config.toml is copied for the config dir, the data dir is copied file by file
    (files that cannot be copied, e.g. a locked cache.db, are skipped with a warning), the
    legacy folders are never deleted, and a warning is returned. Returns
    human-readable messages for the caller to print (empty when nothing happened).
    """
    messages = _migrate_dir(
        _config_dir(LEGACY_APP_NAME), _config_dir(APP_NAME), "config dir", "config.toml"
    )
    old_data, new_data = _data_dir(LEGACY_APP_NAME), _data_dir(APP_NAME)
    if old_data != _config_dir(LEGACY_APP_NAME):  # same dir when LOCALAPPDATA is unset
        messages += _migrate_dir(old_data, new_data, "data dir", None)
    return messages


def load_config() -> dict[str, Any]:
    """Return the parsed config.toml, or {} if it does not exist.

    Raises ``tomllib.TOMLDecodeError`` if the file is malformed.
    """
    path = config_path()
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def get_key(provider_id: str) -> str | None:
    """Return a provider's API key: env `CLIPFORAGE_<ID>_KEY` (legacy `MEMEFINDER_<ID>_KEY`)
    first, then `[keys]`."""
    env_value = _env(f"{provider_id.upper()}_KEY")
    if env_value:
        return env_value
    keys = load_config().get("keys")
    if not isinstance(keys, dict):
        return None
    value = keys.get(provider_id)
    if isinstance(value, str) and value:
        return value
    return None


def get_setting(section: str, key: str, env: str | None = None) -> str | None:
    """Return a string setting: env var first (if non-empty), then `[section] key`.

    ``env`` is the env var name without prefix: ``CLIPFORAGE_<env>`` is tried, then the legacy
    ``MEMEFINDER_<env>``. TOML int/float values are returned as ``str(value)``; bools, tables,
    arrays and empty strings give ``None``.
    """
    if env:
        env_value = _env(env)
        if env_value:
            return env_value
    table = load_config().get(section, {})
    if not isinstance(table, dict):
        return None
    value = table.get(key)
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


def get_bool_setting(section: str, key: str, env: str | None = None, *, default: bool) -> bool:
    """Return a boolean setting: env var first, then a TOML bool ``[section] key``, else default.

    Env values ``1/true/yes/on`` and ``0/false/no/off`` (any case) are recognised; anything else
    (and a non-bool TOML value) falls through to the next source. Raises
    ``tomllib.TOMLDecodeError`` if config.toml is malformed.
    """
    if env:
        env_value = _env(env)
        if env_value:
            lowered = env_value.strip().lower()
            if lowered in _TRUE_STRINGS:
                return True
            if lowered in _FALSE_STRINGS:
                return False
    table = load_config().get(section, {})
    if isinstance(table, dict) and isinstance(table.get(key), bool):
        return bool(table[key])
    return default


def has_key(provider_id: str) -> bool:
    """Return True if an API key is configured for the provider."""
    return get_key(provider_id) is not None


def default_library_dir() -> Path:
    """Default download folder: ``~/Downloads/clipforage``.

    If that does not exist but the pre-rename ``~/Downloads/memefinder`` does, the old folder is
    returned (user downloads are never moved).
    """
    downloads = Path.home() / "Downloads"
    new, old = downloads / APP_NAME, downloads / LEGACY_APP_NAME
    if not new.exists() and old.is_dir():
        return old
    return new


def library_dir() -> Path:
    """Return ``[library] dir`` from config.toml as a Path, else ``default_library_dir()``."""
    value = get_setting("library", "dir")
    return Path(value) if value else default_library_dir()


def _write_config(doc: TOMLDocument) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return path


def set_setting(section: str, key: str, value: str | int | bool | None) -> Path:
    """Set ``[section] key = value`` in config.toml (None removes it); return the file path.

    Other sections and keys, comments and formatting are preserved (edited with tomlkit). Raises
    ``tomllib.TOMLDecodeError`` if the existing file is malformed (it is never overwritten in that
    case).
    """
    load_config()  # validate with tomllib first so a malformed file raises TOMLDecodeError
    path = config_path()
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()
    table = doc.get(section)
    if not isinstance(table, MutableMapping):
        table = tomlkit.table()
        doc[section] = table
    if value is None:
        table.pop(key, None)
    else:
        table[key] = value
    return _write_config(doc)


def set_key(provider_id: str, key: str | None) -> Path:
    """Set ``[keys] <provider_id> = key`` in config.toml (None removes it); return the path."""
    return set_setting("keys", provider_id, key)


def mask_key(key: str) -> str:
    """Mask an API key for display: first 4 + ``…`` + last 4, or ``****`` if under 9 chars."""
    if len(key) < 9:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
