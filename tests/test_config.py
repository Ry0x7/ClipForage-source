import os
import tomllib
from pathlib import Path
from typing import Any

import pytest

from clipforage import config


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("CLIPFORAGE_PEXELS_KEY", raising=False)
    return tmp_path


def write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="APPDATA branch is Windows-only")
def test_config_path_windows_uses_appdata(isolated: Path) -> None:
    assert config.config_path() == isolated / "appdata" / "clipforage" / "config.toml"


def test_config_path_falls_back_to_home(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    assert config.config_path() == isolated / "home" / ".config" / "clipforage" / "config.toml"


def test_missing_file_returns_none(isolated: Path) -> None:
    assert not config.config_path().exists()
    assert config.get_key("pexels") is None
    assert config.has_key("pexels") is False


def test_env_var_wins(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(config.config_path(), '[keys]\npexels = "from-file"\n')
    monkeypatch.setenv("CLIPFORAGE_PEXELS_KEY", "from-env")
    assert config.get_key("pexels") == "from-env"


def test_reads_keys_section(isolated: Path) -> None:
    write_config(config.config_path(), '[keys]\npexels = "from-file"\n')
    assert config.get_key("pexels") == "from-file"
    assert config.has_key("pexels") is True
    assert config.get_key("pixabay") is None


def test_file_without_keys_section(isolated: Path) -> None:
    write_config(config.config_path(), 'other = "x"\n')
    assert config.get_key("pexels") is None


def test_get_setting_env_wins(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(config.config_path(), '[download]\nlibrary = "from-file"\n')
    monkeypatch.setenv("CLIPFORAGE_TEST_LIBRARY", "from-env")
    assert config.get_setting("download", "library", env="TEST_LIBRARY") == "from-env"
    monkeypatch.setenv("CLIPFORAGE_TEST_LIBRARY", "")
    assert config.get_setting("download", "library", env="TEST_LIBRARY") == "from-file"


def test_get_setting_missing_or_non_string(isolated: Path) -> None:
    assert config.get_setting("download", "library") is None
    write_config(
        config.config_path(), '[download]\nlibrary = ""\nflag = true\nlist = [1]\n[download.t]\n'
    )
    assert config.get_setting("download", "library") is None
    assert config.get_setting("download", "flag") is None
    assert config.get_setting("download", "list") is None
    assert config.get_setting("download", "t") is None
    assert config.get_setting("nope", "x") is None


def test_get_setting_numbers_become_strings(isolated: Path) -> None:
    write_config(
        config.config_path(),
        '[download]\nclip_max_wall_s = 120\nratio = 1.5\noff = false\nname = "120"\n',
    )
    assert config.get_setting("download", "clip_max_wall_s") == "120"
    assert config.get_setting("download", "ratio") == "1.5"
    assert config.get_setting("download", "off") is None
    assert config.get_setting("download", "name") == "120"


def test_get_setting_env_wins_over_number(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(config.config_path(), "[download]\nclip_max_wall_s = 120\n")
    monkeypatch.setenv("CLIPFORAGE_CLIP_MAX_WALL_S", "30")
    assert config.get_setting("download", "clip_max_wall_s", env="CLIP_MAX_WALL_S") == "30"


def read_config() -> dict[str, Any]:
    return tomllib.loads(config.config_path().read_text(encoding="utf-8"))


def test_set_key_creates_file_and_parent_dirs(isolated: Path) -> None:
    assert not config.config_path().parent.exists()
    path = config.set_key("pexels", "abc123")
    assert path == config.config_path()
    assert read_config() == {"keys": {"pexels": "abc123"}}


def test_set_key_preserves_other_providers(isolated: Path) -> None:
    config.set_key("pexels", "p-key")
    config.set_key("pixabay", "x-key")
    assert read_config()["keys"] == {"pexels": "p-key", "pixabay": "x-key"}
    config.set_key("pexels", "p-key-2")
    assert read_config()["keys"] == {"pexels": "p-key-2", "pixabay": "x-key"}


def test_set_key_none_removes(isolated: Path) -> None:
    config.set_key("pexels", "p-key")
    config.set_key("pixabay", "x-key")
    config.set_key("pexels", None)
    assert read_config()["keys"] == {"pixabay": "x-key"}
    assert config.get_key("pexels") is None
    config.set_key("never-set", None)  # removing a missing key is a no-op
    assert read_config()["keys"] == {"pixabay": "x-key"}


def test_get_key_sees_written_key(isolated: Path) -> None:
    config.set_key("pexels", "written")
    assert config.get_key("pexels") == "written"
    assert config.has_key("pexels") is True


def test_set_setting_round_trip(isolated: Path) -> None:
    config.set_setting("download", "cookies_from_browser", "firefox")
    config.set_setting("download", "retries", 3)
    config.set_setting("ui", "dark", True)
    assert config.get_setting("download", "cookies_from_browser") == "firefox"
    data = read_config()
    assert data["download"] == {"cookies_from_browser": "firefox", "retries": 3}
    assert data["ui"] == {"dark": True}
    config.set_setting("download", "retries", None)
    assert read_config()["download"] == {"cookies_from_browser": "firefox"}


def test_set_preserves_other_sections(isolated: Path) -> None:
    write_config(
        config.config_path(),
        'top = "level"\n[keys]\npexels = "keep"\n[download]\ncookies_from_browser = "edge"\n',
    )
    config.set_key("pixabay", "new")
    config.set_setting("library", "dir", "D:/media")
    assert read_config() == {
        "top": "level",
        "keys": {"pexels": "keep", "pixabay": "new"},
        "download": {"cookies_from_browser": "edge"},
        "library": {"dir": "D:/media"},
    }


COMMENTED_CONFIG = '# my comment\n[keys]\npexels = "keep"  # inline note\nfreesound = "fs"\n'


def test_set_key_preserves_comments(isolated: Path) -> None:
    path = config.config_path()
    write_config(path, COMMENTED_CONFIG)
    config.set_key("pixabay", "abc")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# my comment\n")
    assert 'pexels = "keep"  # inline note' in text
    assert read_config()["keys"] == {"pexels": "keep", "freesound": "fs", "pixabay": "abc"}


def test_set_key_none_removes_only_that_key_and_keeps_comments(isolated: Path) -> None:
    path = config.config_path()
    write_config(path, COMMENTED_CONFIG)
    config.set_key("freesound", None)
    text = path.read_text(encoding="utf-8")
    assert "# my comment" in text
    assert 'pexels = "keep"  # inline note' in text
    assert "freesound" not in text
    assert read_config() == {"keys": {"pexels": "keep"}}


def test_set_setting_creates_missing_file(isolated: Path) -> None:
    path = config.config_path()
    assert not path.exists()
    assert config.set_setting("library", "dir", "C:\\x") == path
    assert read_config() == {"library": {"dir": "C:\\x"}}


def test_mask_key() -> None:
    assert config.mask_key("abcdefghijwxyz") == "abcd…wxyz"
    assert config.mask_key("123456789") == "1234…6789"
    assert config.mask_key("12345678") == "****"
    assert config.mask_key("") == "****"


def test_library_dir_default_and_override(isolated: Path) -> None:
    assert config.library_dir() == Path.home() / "Downloads" / "clipforage"
    assert config.library_dir() == config.default_library_dir()
    target = isolated / "my library"
    config.set_setting("library", "dir", str(target))
    assert config.library_dir() == target
