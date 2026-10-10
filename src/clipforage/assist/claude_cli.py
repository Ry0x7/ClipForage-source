"""Assist backend: the locally installed Claude Code CLI (``claude -p``).

Reuses the user's existing Claude Code login; ClipForage never sees a key or token. The call is
a single-turn text completion: our own system prompt replaces Claude Code's, every built-in
tool is disabled, ``--safe-mode`` turns off CLAUDE.md, skills, plugins, hooks and MCP servers,
and the process runs in an empty temp dir so no project files are in reach.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from clipforage import config
from clipforage.assist.base import AssistError, BackendStatus, configured_model

ID = "claude_cli"
DEFAULT_MODEL = "claude-opus-5-5"
_VERSION_TIMEOUT_S = 15.0
_STDERR_TAIL_CHARS = 600

Runner = Callable[[list[str], Path, float], tuple[int, str, str]]
"""``runner(argv, cwd, timeout_s) -> (returncode, stdout, stderr)``; raises ``TimeoutError``."""

# Flags that make `claude -p` a cheap single-turn completion (checked against `claude --help`).
ISOLATION_FLAGS: tuple[str, ...] = (
    "--tools",
    "",  # "" disables every built-in tool
    "--safe-mode",  # no CLAUDE.md, skills, plugins, hooks, MCP servers or custom agents
    "--strict-mcp-config",  # and no MCP servers from any config (no --mcp-config is given)
    "--no-session-persistence",  # nothing written to the session history
    "--effort",
    "low",
)


def find_claude() -> Path | None:
    """Return the ``claude`` executable: ``PATH`` first, then ``~/.local/bin``."""
    found = shutil.which("claude")
    if found:
        return Path(found)
    local_bin = Path.home() / ".local" / "bin"
    for name in ("claude.exe", "claude") if os.name == "nt" else ("claude",):
        candidate = local_bin / name
        if candidate.is_file():
            return candidate
    return None


def _run_subprocess(argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"timed out after {timeout_s:g}s") from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def version(path: Path, *, runner: Runner | None = None) -> str | None:
    """Return the Claude Code version (``claude --version``), or None if it cannot be read."""
    run = runner or _run_subprocess
    try:
        code, out, _ = run([str(path), "--version"], Path.cwd(), _VERSION_TIMEOUT_S)
    except (OSError, TimeoutError):
        return None
    match = re.search(r"\d+\.\d+\.\d+", out)
    return match.group(0) if code == 0 and match else None


def status(*, runner: Runner | None = None) -> BackendStatus:
    """Report whether the Claude Code CLI is installed (login is only checked by a real call)."""
    path = find_claude()
    model = configured_model(DEFAULT_MODEL, ID)
    if path is None:
        return BackendStatus(
            id=ID,
            available=False,
            detail="Claude Code CLI not found: install it from https://claude.com/claude-code "
            "and run `claude` once to log in",
            model=model,
        )
    ver = version(path, runner=runner)
    label = f"Claude Code {ver}" if ver else "Claude Code (version unknown)"
    return BackendStatus(id=ID, available=True, detail=f"{label} at {path}", model=model)


def build_argv(claude: Path, system: str, user: str, model: str) -> list[str]:
    """Return the argv for one isolated, tool-less ``claude -p`` call."""
    return [
        str(claude),
        "-p",
        user,
        "--output-format",
        "json",
        "--model",
        model,
        "--system-prompt",
        system,
        *ISOLATION_FLAGS,
    ]


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _STDERR_TAIL_CHARS else "..." + text[-_STDERR_TAIL_CHARS:]


def _with_stderr(message: str, stderr: str) -> str:
    tail = _tail(stderr)
    return f"{message}\nstderr: {tail}" if tail else message


def run(
    system: str,
    user: str,
    *,
    model: str,
    timeout_s: float = 90.0,
    runner: Runner | None = None,
    claude: Path | None = None,
) -> tuple[str, float | None, str | None]:
    """Run one completion; return ``(result_text, cost_usd, model_used)``.

    Raises ``AssistError`` when Claude Code is missing, times out, or reports an error.
    """
    exe = claude or find_claude()
    if exe is None:
        raise AssistError(
            "Claude Code CLI not found: install it from https://claude.com/claude-code "
            "and run `claude` once to log in"
        )
    argv = build_argv(exe, system, user, model)
    base = config.data_dir() / "assist"
    base.mkdir(parents=True, exist_ok=True)
    run_cmd = runner or _run_subprocess
    with tempfile.TemporaryDirectory(dir=base, ignore_cleanup_errors=True) as tmp:
        try:
            code, stdout, stderr = run_cmd(argv, Path(tmp), timeout_s)
        except TimeoutError as exc:
            raise AssistError(f"claude -p {exc}") from None
        except OSError as exc:
            raise AssistError(f"could not run {exe}: {exc}") from None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise AssistError(
            _with_stderr(f"claude -p exited {code} without JSON output: {_tail(stdout)!r}", stderr)
        ) from None
    if not isinstance(payload, dict):
        raise AssistError(_with_stderr("claude -p returned unexpected JSON", stderr))
    cost = payload.get("total_cost_usd")
    cost_usd = float(cost) if isinstance(cost, int | float) else None
    if payload.get("is_error") or payload.get("subtype") != "success":
        detail = payload.get("result") or payload.get("subtype") or f"exit code {code}"
        raise AssistError(_with_stderr(f"claude -p failed: {detail}", stderr))
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        raise AssistError(_with_stderr("claude -p returned an empty result", stderr))
    usage = payload.get("modelUsage")
    model_used = next(iter(usage), None) if isinstance(usage, dict) and usage else None
    return result, cost_usd, model_used or model
