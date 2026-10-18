"""User configuration: API keys from env vars or config.toml (read and write)."""

import os
import tomllib
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument

APP_NAME = "clipforage"
ENV_PREFIX = "CLIPFORAGE_"


def _env(name_suffix: str) -> str | None:
    """Return ``CLIPFORAGE_<suffix>`` if set and non-empty."""
    return os.environ.get(ENV_PREFIX + name_suffix) or None


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
    """Return a provider's API key: env `CLIPFORAGE_<ID>_KEY` first, then `[keys]`."""
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

    ``env`` is the env var name without prefix: ``CLIPFORAGE_<env>``.
    TOML int/float values are returned as ``str(value)``; bools, tables,
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
    """Default download folder: ``~/Downloads/clipforage``."""
    return Path.home() / "Downloads" / APP_NAME


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
