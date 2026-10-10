"""Find and install the runtime binaries yt-dlp needs: ffmpeg and Deno.

``clipforage setup`` downloads official releases into ``data_dir() / "bin"``; every archive is
verified against its published SHA-256 checksum before anything is extracted (fail closed).
"""

import hashlib
import importlib.util
import os
import platform
import re
import shutil
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import yt_dlp.version

from clipforage.config import data_dir

BIN_DIR_NAME = "bin"

Progress = Callable[[int, int | None], None]

_DENO_BASE = "https://github.com/denoland/deno/releases/latest/download"
_BTBN_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
_BTBN_CHECKSUMS = f"{_BTBN_BASE}/checksums.sha256"
_SHA256_RE = re.compile(r"\b[0-9A-Fa-f]{64}\b")

_DENO_TARGETS: dict[tuple[str, str], str] = {
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("windows", "arm64"): "aarch64-pc-windows-msvc",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("darwin", "arm64"): "aarch64-apple-darwin",
}
_BTBN_TARGETS: dict[tuple[str, str], str] = {
    ("windows", "x86_64"): "win64",
    ("windows", "arm64"): "winarm64",
    ("linux", "x86_64"): "linux64",
    ("linux", "arm64"): "linuxarm64",
}
_MACHINE_ALIASES: dict[str, str] = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

MACOS_FFMPEG_HINT = (
    "ffmpeg: no verified official build for macOS; install with: brew install ffmpeg"
)


class SetupError(Exception):
    """Installing a binary failed; the message is user-facing."""


@dataclass(frozen=True)
class Release:
    """One downloadable archive: ``name`` is the asset file name, ``members`` the files to keep."""

    name: str
    url: str
    checksum_url: str | None
    members: tuple[str, ...]


def bin_dir() -> Path:
    """Return the folder where ``setup`` puts binaries (not created here)."""
    return data_dir() / BIN_DIR_NAME


def exe(name: str) -> str:
    """Return the executable file name for ``name`` on this OS."""
    return name + ".exe" if os.name == "nt" else name


def find_tool(name: str) -> Path | None:
    """Return the tool path: ``bin_dir()`` first (setup wins over PATH), then PATH."""
    local = bin_dir() / exe(name)
    if local.is_file():
        return local
    found = shutil.which(name)
    return Path(found) if found else None


def releases_for(
    system: str = platform.system(), machine: str = platform.machine()
) -> dict[str, Release]:
    """Return the official releases for this platform, keyed by tool (``ffmpeg``, ``deno``)."""
    key = (system.lower(), _MACHINE_ALIASES.get(machine.lower(), machine.lower()))
    windows = key[0] == "windows"
    releases: dict[str, Release] = {}
    btbn = _BTBN_TARGETS.get(key)
    if btbn is not None:
        ext = ".zip" if windows else ".tar.xz"
        asset = f"ffmpeg-master-latest-{btbn}-gpl{ext}"
        releases["ffmpeg"] = Release(
            name=asset,
            url=f"{_BTBN_BASE}/{asset}",
            checksum_url=_BTBN_CHECKSUMS,
            members=(
                ("bin/ffmpeg.exe", "bin/ffprobe.exe") if windows else ("bin/ffmpeg", "bin/ffprobe")
            ),
        )
    target = _DENO_TARGETS.get(key)
    if target is not None:
        asset = f"deno-{target}.zip"
        url = f"{_DENO_BASE}/{asset}"
        releases["deno"] = Release(
            name=asset,
            url=url,
            checksum_url=url + ".sha256sum",
            members=("deno.exe",) if windows else ("deno",),
        )
    return releases


def _names_asset(line: str, asset_name: str) -> bool:
    for token in line.split():
        token = token.lstrip("*")
        if re.split(r"[\\/]", token)[-1] == asset_name:
            return True
    return False


def parse_sha256(text: str, asset_name: str | None) -> str | None:
    """Return the lowercase SHA-256 from a checksum file, or None.

    Handles ``sha256sum`` lines (single or aggregate) and PowerShell ``Get-FileHash`` tables.
    With ``asset_name``, a line naming exactly that file wins; if no line names it, a file
    holding exactly one hash is accepted (asset-specific checksum files).
    """
    hits = [(line, m.group(0)) for line in text.splitlines() if (m := _SHA256_RE.search(line))]
    if not hits:
        return None
    if asset_name:
        for line, digest in hits:
            if _names_asset(line, asset_name):
                return digest.lower()
    if len(hits) == 1:
        return hits[0][1].lower()
    return None


def download_file(
    url: str, dest: Path, *, client: httpx.Client, progress: Progress | None = None
) -> str:
    """Stream ``url`` to ``dest`` (via a ``.part`` file) and return its SHA-256 hex digest."""
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            length = response.headers.get("content-length")
            total = int(length) if length and length.isdigit() else None
            done = 0
            with part.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        os.replace(part, dest)
    except httpx.HTTPStatusError as exc:
        part.unlink(missing_ok=True)
        raise SetupError(f"download failed: HTTP {exc.response.status_code} for {url}") from exc
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise SetupError(f"download failed for {url}: {exc}") from exc
    return digest.hexdigest()


def _matches(name: str, suffix: str) -> bool:
    name = name.replace("\\", "/")
    return name == suffix or name.endswith("/" + suffix)


def extract_members(archive: Path, members: tuple[str, ...], dest_dir: Path) -> list[Path]:
    """Extract only ``members`` (matched by path suffix) flat into ``dest_dir``.

    Targets are ``dest_dir / basename``, so archive paths can never escape ``dest_dir``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(suffix: str, data: bytes) -> None:
        target = dest_dir / Path(suffix).name
        target.write_bytes(data)
        if os.name != "nt":
            target.chmod(0o755)
        written.append(target)

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            for suffix in members:
                info = next((i for i in infos if _matches(i.filename, suffix)), None)
                if info is None:
                    raise SetupError(f"{suffix} not found in {archive.name}")
                write(suffix, zf.read(info))
    elif archive.name.endswith((".tar.xz", ".tar.gz", ".tar")):
        with tarfile.open(archive) as tf:
            infos = [i for i in tf.getmembers() if i.isfile()]
            for suffix in members:
                info = next((i for i in infos if _matches(i.name, suffix)), None)
                fh = tf.extractfile(info) if info is not None else None
                if fh is None:
                    raise SetupError(f"{suffix} not found in {archive.name}")
                with fh:
                    write(suffix, fh.read())
    else:
        raise SetupError(f"unsupported archive type: {archive.name}")
    return written


def install(
    tool: str,
    *,
    client: httpx.Client | None = None,
    echo: Callable[[str], None] = print,
    force: bool = False,
    progress: Progress | None = None,
) -> Path:
    """Download, verify and extract the official release of ``tool`` into ``bin_dir()``."""
    target = bin_dir() / exe(tool)
    if target.is_file() and not force:
        echo(f"{tool}: already installed at {target}")
        return target
    release = releases_for().get(tool)
    if release is None:
        raise SetupError(f"no verified official {tool} build for this platform")
    own_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=60)
    archive = bin_dir() / release.name
    try:
        bin_dir().mkdir(parents=True, exist_ok=True)
        actual = download_file(release.url, archive, client=http, progress=progress)
        if release.checksum_url:
            try:
                response = http.get(release.checksum_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise SetupError(
                    f"could not fetch checksum for {release.name}: {exc}; not installing"
                ) from exc
            expected = parse_sha256(response.text, release.name)
            if expected is None:
                raise SetupError(f"could not parse checksum for {release.name}; not installing")
            if expected != actual:
                raise SetupError(f"checksum mismatch for {release.name}")
        extract_members(archive, release.members, bin_dir())
    except httpx.HTTPError as exc:
        raise SetupError(f"{tool}: network error: {exc}; check your connection and retry") from exc
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise SetupError(f"{tool}: corrupt archive {release.name}: {exc}; retry setup") from exc
    except OSError as exc:
        raise SetupError(f"{tool}: file error: {exc}; check free space and permissions") from exc
    finally:
        archive.unlink(missing_ok=True)
        if own_client:
            http.close()
    return target


def ytdlp_version() -> str:
    """Return the installed yt-dlp version."""
    return yt_dlp.version.__version__


def has_ejs() -> bool:
    """Return True if ``yt-dlp-ejs`` (YouTube JS challenge solver scripts) is installed."""
    return importlib.util.find_spec("yt_dlp_ejs") is not None
