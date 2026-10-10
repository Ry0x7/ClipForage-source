import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from clipforage import assist
from clipforage.assist import anthropic_api, base, claude_cli, openai_api
from clipforage.assist.prompt import RESULT_SCHEMA, build_prompt, parse_result
from clipforage.cli import app
from clipforage.models import AssistResult, Kind

ANSWER = {
    "queries": ["woman yelling at cat", "woman yelling at cat green screen"],
    "names": ["Woman Yelling at a Cat"],
    "kind_hint": "image",
    "notes": "A 2019 meme pairing a Real Housewives still with a cat at a dinner table.",
    "rights_note": None,
}
ANSWER_TEXT = json.dumps(ANSWER)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CLIPFORAGE_ASSIST_BACKEND",
        "CLIPFORAGE_ASSIST_MODEL",
        "CLIPFORAGE_ASSIST_CLAUDE_CLI_MODEL",
        "CLIPFORAGE_ASSIST_ANTHROPIC_MODEL",
        "CLIPFORAGE_ASSIST_OPENAI_MODEL",
        "CLIPFORAGE_ASSIST_TIMEOUT_S",
        "CLIPFORAGE_ASSIST_ENABLED",
        "CLIPFORAGE_ANTHROPIC_KEY",
        "CLIPFORAGE_OPENAI_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CONFIG_DIR",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# --- prompt / parse_result -------------------------------------------------------------------


def test_build_prompt_includes_kind_and_style() -> None:
    system, user = build_prompt("  skeleton battle song ", Kind.AUDIO_MUSIC, "meme")
    assert "ONLY a JSON object" in system
    assert "green screen" in system
    for kind in Kind:
        assert kind.value in system
    assert user.splitlines() == [
        "Description: skeleton battle song",
        "Wanted kind: audio_music",
        "Editing style: meme",
    ]
    _, plain = build_prompt("x", None, "any")
    assert plain == "Description: x"


def test_parse_result_clean_json() -> None:
    result = parse_result(ANSWER_TEXT, "claude_cli", "m1")
    assert result.queries == ANSWER["queries"]
    assert result.names == ["Woman Yelling at a Cat"]
    assert result.kind_hint is Kind.IMAGE
    assert result.backend == "claude_cli"
    assert result.model == "m1"
    assert result.cost_usd is None


def test_parse_result_fenced_json() -> None:
    raw = f"```json\n{ANSWER_TEXT}\n```"
    assert parse_result(raw, "openai", None).queries == ANSWER["queries"]


def test_parse_result_prose_then_json_with_braces_in_strings() -> None:
    answer = dict(ANSWER, notes='Uses {braces} and a "quote" inside.')
    raw = f"Sure! Here is the object you asked for:\n{json.dumps(answer)}\nHope that helps {{"
    result = parse_result(raw, "anthropic", "m")
    assert result.notes == 'Uses {braces} and a "quote" inside.'


def test_parse_result_unknown_kind_is_dropped_and_queries_capped() -> None:
    answer = dict(ANSWER, kind_hint="meme", queries=[f"q{i}" for i in range(9)] + ["  "])
    result = parse_result(json.dumps(answer), "x", None)
    assert result.kind_hint is None
    assert result.queries == [f"q{i}" for i in range(6)]


@pytest.mark.parametrize(
    "raw",
    [
        "I could not find anything.",
        "{not json at all}",
        json.dumps({"names": ["x"]}),  # queries missing
        json.dumps(dict(ANSWER, queries=[])),
        json.dumps(dict(ANSWER, queries="one string")),
    ],
)
def test_parse_result_invalid_raises(raw: str) -> None:
    with pytest.raises(assist.AssistError, match="claude_cli"):
        parse_result(raw, "claude_cli", None)


def test_result_schema_matches_model_fields() -> None:
    backend_only = {"backend", "model", "cost_usd"}
    assert set(RESULT_SCHEMA["properties"]) == set(AssistResult.model_fields) - backend_only
    assert set(RESULT_SCHEMA["required"]) == set(RESULT_SCHEMA["properties"])
    assert RESULT_SCHEMA["additionalProperties"] is False


# --- claude_cli -------------------------------------------------------------------------------


def _cli_payload(**overrides: Any) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": ANSWER_TEXT,
        "total_cost_usd": 0.0123,
        "modelUsage": {"claude-opus-5-5": {"inputTokens": 900, "outputTokens": 120}},
    }
    payload.update(overrides)
    return json.dumps(payload)


class FakeRunner:
    def __init__(self, code: int, stdout: str, stderr: str = "") -> None:
        self.result = (code, stdout, stderr)
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, str]:
        self.calls.append((argv, cwd, timeout_s))
        assert cwd.is_dir()
        return self.result


def test_claude_cli_success_argv_and_isolation(tmp_path: Path) -> None:
    runner = FakeRunner(0, _cli_payload())
    exe = tmp_path / "claude.exe"
    text, cost, model = claude_cli.run(
        "SYS", "USER", model="claude-opus-5-5", timeout_s=12, runner=runner, claude=exe
    )
    assert text == ANSWER_TEXT
    assert cost == pytest.approx(0.0123)
    assert model == "claude-opus-5-5"
    argv, cwd, timeout = runner.calls[0]
    assert argv[0] == str(exe)
    assert argv[argv.index("-p") + 1] == "USER"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--tools") + 1] == ""
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--effort") + 1] == "low"
    assert timeout == 12
    # a fresh temp dir under the data dir, removed afterwards
    from clipforage import config

    assert cwd.parent == config.data_dir() / "assist"
    assert not cwd.exists()


def test_claude_cli_is_error_includes_stderr_tail(tmp_path: Path) -> None:
    runner = FakeRunner(
        1,
        _cli_payload(subtype="success", is_error=True, result="Not logged in"),
        "line1\nPlease run /login",
    )
    with pytest.raises(assist.AssistError) as err:
        claude_cli.run("s", "u", model="m", runner=runner, claude=tmp_path / "claude")
    assert "Not logged in" in str(err.value)
    assert "Please run /login" in str(err.value)


def test_claude_cli_non_success_subtype_and_non_json(tmp_path: Path) -> None:
    runner = FakeRunner(0, _cli_payload(subtype="error_max_turns", result=None))
    with pytest.raises(assist.AssistError, match="error_max_turns"):
        claude_cli.run("s", "u", model="m", runner=runner, claude=tmp_path / "claude")
    runner = FakeRunner(2, "error: unknown option '--safe-mode'", "boom")
    with pytest.raises(assist.AssistError, match="without JSON output") as err:
        claude_cli.run("s", "u", model="m", runner=runner, claude=tmp_path / "claude")
    assert "boom" in str(err.value)


def test_claude_cli_timeout(tmp_path: Path) -> None:
    def slow(argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, str]:
        raise TimeoutError(f"timed out after {timeout_s:g}s")

    with pytest.raises(assist.AssistError, match="timed out after 5s"):
        claude_cli.run("s", "u", model="m", timeout_s=5, runner=slow, claude=tmp_path / "c")


def test_claude_cli_status_and_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "claude.exe"
    monkeypatch.setattr(claude_cli, "find_claude", lambda: exe)
    status = claude_cli.status(runner=FakeRunner(0, "2.1.287 (Claude Code)\n"))
    assert status.available
    assert status.detail == f"Claude Code 2.1.287 at {exe}"
    assert status.model == "claude-opus-5-5"
    monkeypatch.setattr(claude_cli, "find_claude", lambda: None)
    missing = claude_cli.status()
    assert not missing.available
    assert "not found" in missing.detail


def test_find_claude_falls_back_to_local_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    assert claude_cli.find_claude() is None
    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True)
    exe = local_bin / ("claude.exe" if claude_cli.os.name == "nt" else "claude")
    exe.write_text("")
    assert claude_cli.find_claude() == exe


# --- anthropic --------------------------------------------------------------------------------


class FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.params: dict[str, Any] = {}

    def create(self, **params: Any) -> Any:
        self.params = params
        return self.response


def _message(text: str, stop_reason: str = "end_turn", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        **extra,
    )


def test_anthropic_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = FakeMessages(_message(ANSWER_TEXT))
    seen: list[tuple[str | None, float]] = []

    def factory(api_key: str | None, timeout_s: float) -> Any:
        seen.append((api_key, timeout_s))
        return SimpleNamespace(messages=messages)

    text, cost, model = anthropic_api.run(
        "SYS", "USER", model="claude-opus-5-5", timeout_s=30, client_factory=factory
    )
    assert (text, cost, model) == (ANSWER_TEXT, None, "claude-opus-5-5")
    assert seen == [(None, 30)]  # no ClipForage key: zero-arg client (env / ant profile)
    params = messages.params
    assert params["model"] == "claude-opus-5-5"
    assert params["system"] == "SYS"
    assert params["messages"] == [{"role": "user", "content": "USER"}]
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": RESULT_SCHEMA}
    flat = json.dumps(params)
    assert "budget_tokens" not in flat
    assert "tool_choice" not in params
    assert "thinking" not in params

    monkeypatch.setenv("CLIPFORAGE_ANTHROPIC_KEY", "sk-test-key")
    anthropic_api.run("s", "u", model="m", client_factory=factory)
    assert seen[-1][0] == "sk-test-key"


def test_anthropic_refusal() -> None:
    details = SimpleNamespace(category="cyber", explanation="x")
    messages = FakeMessages(_message("", stop_reason="refusal", stop_details=details))
    with pytest.raises(assist.AssistError, match="declined.*cyber"):
        anthropic_api.run(
            "s", "u", model="m", client_factory=lambda k, t: SimpleNamespace(messages=messages)
        )


def test_anthropic_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(anthropic_api, "_package_installed", lambda: False)
    status = anthropic_api.status()
    assert not status.available
    assert "pip install clipforage[assist]" in status.detail
    monkeypatch.setattr(anthropic_api, "_package_installed", lambda: True)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "ant"))
    assert not anthropic_api.status().available
    creds = tmp_path / "ant" / "credentials"
    creds.mkdir(parents=True)
    (creds / "default.json").write_text("{}")
    assert anthropic_api.status().detail == "`ant auth login` profile found"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert anthropic_api.status().detail == "ANTHROPIC_API_KEY set"


def test_anthropic_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic_api, "_package_installed", lambda: False)
    with pytest.raises(assist.AssistError, match=r"pip install clipforage\[assist\]"):
        anthropic_api.run("s", "u", model="m")


# --- openai -----------------------------------------------------------------------------------


@respx.mock
def test_openai_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_OPENAI_KEY", "sk-openai")
    route = respx.post(openai_api.API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini-2024-07-18",
                "choices": [{"message": {"role": "assistant", "content": ANSWER_TEXT}}],
            },
        )
    )
    text, cost, model = openai_api.run("SYS", "USER", model="gpt-4o-mini")
    assert (text, cost, model) == (ANSWER_TEXT, None, "gpt-4o-mini-2024-07-18")
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-openai"
    body = json.loads(request.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]


@respx.mock
def test_openai_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_OPENAI_KEY", "sk-bad")
    respx.post(openai_api.API_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
    )
    with pytest.raises(assist.AssistError, match="key invalid"):
        openai_api.run("s", "u", model="gpt-4o-mini")


def test_openai_no_key() -> None:
    assert not openai_api.status().available
    with pytest.raises(assist.AssistError, match="CLIPFORAGE_OPENAI_KEY"):
        openai_api.run("s", "u", model="gpt-4o-mini")


def test_openai_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert openai_api.status().detail == "OPENAI_API_KEY set"
    assert openai_api.api_key() == "sk-env"


# --- describe / auto / config ---------------------------------------------------------------


def _patch_statuses(
    monkeypatch: pytest.MonkeyPatch, available: set[str], *, enabled: bool = True
) -> list[str]:
    if enabled:
        monkeypatch.setenv("CLIPFORAGE_ASSIST_ENABLED", "1")
    calls: list[str] = []
    for module in (claude_cli, anthropic_api, openai_api):

        def status(module: Any = module, **_: Any) -> base.BackendStatus:
            return base.BackendStatus(
                id=module.ID,
                available=module.ID in available,
                detail=f"{module.ID} detail",
                model=base.configured_model(module.DEFAULT_MODEL),
            )

        def run(system: str, user: str, *, model: str, timeout_s: float, module: Any = module):
            calls.append(f"{module.ID}:{model}:{timeout_s:g}")
            return ANSWER_TEXT, 0.5 if module.ID == "claude_cli" else None, model

        monkeypatch.setattr(module, "status", status)
        monkeypatch.setattr(module, "run", run)
    return calls


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        ({"claude_cli", "anthropic", "openai"}, "claude_cli"),
        ({"anthropic", "openai"}, "anthropic"),
        ({"openai"}, "openai"),
    ],
)
def test_auto_order(monkeypatch: pytest.MonkeyPatch, available: set[str], expected: str) -> None:
    calls = _patch_statuses(monkeypatch, available)
    result = assist.describe("cat at dinner", backend="auto")
    assert result.backend == expected
    assert calls[0].split(":")[0] == expected


def test_auto_none_available_lists_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, set())
    with pytest.raises(assist.AssistError) as err:
        assist.describe("cat")
    message = str(err.value)
    for needle in ("claude_cli detail", "openai detail", "ant auth login", "CLIPFORAGE_OPENAI_KEY"):
        assert needle in message


def test_explicit_backend_unavailable_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, {"claude_cli"})
    with pytest.raises(assist.AssistError, match="openai backend unavailable: openai detail"):
        assist.describe("cat", backend="openai")
    with pytest.raises(assist.AssistError, match="unknown assist backend 'gemini'"):
        assist.describe("cat", backend="gemini")


def test_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_statuses(monkeypatch, {"claude_cli", "openai"})
    monkeypatch.setenv("CLIPFORAGE_ASSIST_BACKEND", "openai")
    monkeypatch.setenv("CLIPFORAGE_ASSIST_MODEL", "gpt-x")
    monkeypatch.setenv("CLIPFORAGE_ASSIST_TIMEOUT_S", "7")
    result = assist.describe("cat")
    assert result.backend == "openai"
    assert result.model == "gpt-x"
    assert calls == ["openai:gpt-x:7"]


def test_config_file_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforage import config

    config.set_setting("assist", "backend", "claude_cli")
    config.set_setting("assist", "timeout_s", 33)
    assert base.configured_backend() == "claude_cli"
    assert base.configured_timeout() == 33
    assert base.configured_model("dflt") == "dflt"
    monkeypatch.setenv("CLIPFORAGE_ASSIST_TIMEOUT_S", "nonsense")
    assert base.configured_timeout() == base.DEFAULT_TIMEOUT_S


def test_describe_returns_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, {"claude_cli"})
    result = assist.describe("cat", kind=Kind.VIDEO, style="meme")
    assert result.cost_usd == 0.5
    assert result.model == "claude-opus-5-5"


# --- CLI ----------------------------------------------------------------------------------------

runner = CliRunner()


def test_cli_describe_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, {"claude_cli"})
    result = runner.invoke(app, ["describe", "cat at dinner", "--json", "--style", "meme"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["backend"] == "claude_cli"
    assert data["queries"] == ANSWER["queries"]
    assert data["cost_usd"] == 0.5


def test_cli_describe_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, {"openai"})
    result = runner.invoke(app, ["describe", "cat", "--backend", "openai"])
    assert result.exit_code == 0, result.output
    assert "backend: openai (gpt-4o-mini)" in result.output
    assert "  1. woman yelling at cat" in result.output
    assert "names:   Woman Yelling at a Cat" in result.output
    assert "cost:    not reported" in result.output


def test_cli_describe_unavailable_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, set())
    result = runner.invoke(app, ["describe", "cat"])
    assert result.exit_code == 1
    assert "no assist backend available" in result.output
    assert "pip install clipforage[assist]" in result.output


def test_cli_describe_bad_kind() -> None:
    result = runner.invoke(app, ["describe", "cat", "--kind", "meme"])
    assert result.exit_code == 1
    assert "invalid kind" in result.output


# --- enabled gate -------------------------------------------------------------------------------


def test_assist_disabled_by_default_and_never_calls_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_statuses(monkeypatch, {"claude_cli", "openai"}, enabled=False)
    assert base.configured_enabled() is False
    with pytest.raises(assist.AssistDisabledError) as err:
        assist.describe("cat", backend="claude_cli")
    assert str(err.value) == base.DISABLED_MESSAGE
    assert isinstance(err.value, assist.AssistError)
    assert calls == []


@pytest.mark.parametrize(
    ("env", "enabled"),
    [("1", True), ("true", True), ("YES", True), ("0", False), ("no", False), ("off", False)],
)
def test_assist_enabled_env(monkeypatch: pytest.MonkeyPatch, env: str, enabled: bool) -> None:
    monkeypatch.setenv("CLIPFORAGE_ASSIST_ENABLED", env)
    assert base.configured_enabled() is enabled


def test_assist_enabled_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforage import config

    config.set_setting("assist", "enabled", True)
    assert base.configured_enabled() is True
    config.set_setting("assist", "enabled", False)
    assert base.configured_enabled() is False
    monkeypatch.setenv("CLIPFORAGE_ASSIST_ENABLED", "1")  # env wins
    assert base.configured_enabled() is True


def test_cli_describe_disabled_exits_1_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_statuses(monkeypatch, {"claude_cli"}, enabled=False)
    result = runner.invoke(app, ["describe", "cat"])
    assert result.exit_code == 1
    assert "Assist is disabled" in result.output
    assert "enabled = true" in result.output and "CLIPFORAGE_ASSIST_ENABLED=1" in result.output
    assert calls == []


# --- auto fall-through on auth failures --------------------------------------------------------


def _fail_backend(monkeypatch: pytest.MonkeyPatch, module: Any, message: str) -> None:
    def run(system: str, user: str, *, model: str, timeout_s: float) -> Any:
        raise base.AssistError(message)

    monkeypatch.setattr(module, "run", run)


@pytest.mark.parametrize(
    "message",
    [
        "claude -p failed: Invalid API key · Please run /login",
        "claude -p exited 1 without JSON output: ''\nstderr: Not logged in",
        "claude -p failed: OAuth token has expired",
        "Anthropic API error 401: authentication_error",
    ],
)
def test_auto_falls_through_auth_errors(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    calls = _patch_statuses(monkeypatch, {"claude_cli", "openai"})
    _fail_backend(monkeypatch, claude_cli, message)
    result = assist.describe("cat", backend="auto")
    assert result.backend == "openai"
    assert calls == ["openai:gpt-4o-mini:90"]


def test_auto_reraises_non_auth_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_statuses(monkeypatch, {"claude_cli", "openai"})
    _fail_backend(monkeypatch, claude_cli, "claude -p timed out after 90s")
    with pytest.raises(assist.AssistError, match="timed out"):
        assist.describe("cat", backend="auto")
    assert calls == []


def test_auto_all_auth_failures_are_summarised(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, {"claude_cli", "openai"})
    _fail_backend(monkeypatch, claude_cli, "claude -p failed: Not logged in")
    _fail_backend(monkeypatch, openai_api, "OpenAI API error 401: Incorrect API key")
    with pytest.raises(assist.AssistError) as err:
        assist.describe("cat", backend="auto")
    message = str(err.value)
    assert "claude_cli: claude -p failed: Not logged in" in message
    assert "openai: OpenAI API error 401" in message


def test_explicit_backend_does_not_fall_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_statuses(monkeypatch, {"claude_cli", "openai"})
    _fail_backend(monkeypatch, claude_cli, "claude -p failed: Not logged in")
    with pytest.raises(assist.AssistError, match="Not logged in"):
        assist.describe("cat", backend="claude_cli")
    assert calls == []


def test_unavailable_errors_are_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, set())
    with pytest.raises(assist.AssistUnavailableError):
        assist.describe("cat")
    with pytest.raises(assist.AssistUnavailableError):
        assist.describe("cat", backend="openai")


def test_auto_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_statuses(monkeypatch, {"anthropic", "openai"})
    assert assist.auto_pick() == "anthropic"
    _patch_statuses(monkeypatch, set())
    assert assist.auto_pick() is None


# --- model family ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "backend", "expected"),
    [
        ("claude-sonnet-5", "openai", "gpt-4o-mini"),  # claude model on openai: ignored
        ("gpt-5", "claude_cli", "claude-opus-5-5"),
        ("o3-mini", "anthropic", "claude-opus-5-5"),
        ("gpt-5", "openai", "gpt-5"),
        ("claude-haiku-5", "claude_cli", "claude-haiku-5"),
        ("opus", "claude_cli", "opus"),  # Claude Code alias, not an o-series model
        ("opus", "openai", "gpt-4o-mini"),
        ("my-custom-model", "openai", "my-custom-model"),
    ],
)
def test_model_family_mismatch_uses_backend_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configured: str,
    backend: str,
    expected: str,
) -> None:
    calls = _patch_statuses(monkeypatch, {backend})
    monkeypatch.setenv("CLIPFORAGE_ASSIST_MODEL", configured)
    with caplog.at_level("WARNING"):
        result = assist.describe("cat", backend=backend)
    assert calls == [f"{backend}:{expected}:90"]
    assert result.model == expected
    assert ("ignoring [assist] model" in caplog.text) is (expected != configured)


# --- per-backend model ------------------------------------------------------------------------


def test_per_backend_model_wins_and_skips_family_check(caplog: pytest.LogCaptureFixture) -> None:
    from clipforage import config

    config.set_setting("assist", "model", "gpt-5")
    config.set_setting("assist", "openai_model", "claude-x")  # explicit: no family check
    config.set_setting("assist", "anthropic_model", "claude-sonnet-5")
    with caplog.at_level("WARNING"):
        assert base.configured_model("gpt-4o-mini", "openai") == "claude-x"
        assert base.configured_model("claude-opus-5-5", "anthropic") == "claude-sonnet-5"
    assert "ignoring [assist] model" not in caplog.text
    # backend=None ignores per-backend keys (unchanged behaviour).
    assert base.configured_model("dflt") == "gpt-5"


def test_per_backend_model_env_overrides_file(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforage import config

    config.set_setting("assist", "claude_cli_model", "claude-haiku-5")
    assert base.configured_model("claude-opus-5-5", "claude_cli") == "claude-haiku-5"
    monkeypatch.setenv("CLIPFORAGE_ASSIST_CLAUDE_CLI_MODEL", "sonnet")
    assert base.configured_model("claude-opus-5-5", "claude_cli") == "sonnet"


def test_per_backend_model_absent_falls_back_to_shared(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from clipforage import config

    config.set_setting("assist", "model", "claude-sonnet-5")
    config.set_setting("assist", "anthropic_model", "")  # empty counts as unset
    with caplog.at_level("WARNING"):
        assert base.configured_model("claude-opus-5-5", "anthropic") == "claude-sonnet-5"
        assert base.configured_model("gpt-4o-mini", "openai") == "gpt-4o-mini"
    assert "ignoring [assist] model 'claude-sonnet-5' for the openai backend" in caplog.text


def test_per_backend_model_reaches_describe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_statuses(monkeypatch, {"openai"})
    monkeypatch.setenv("CLIPFORAGE_ASSIST_MODEL", "gpt-5")
    monkeypatch.setenv("CLIPFORAGE_ASSIST_OPENAI_MODEL", "claude-x")
    result = assist.describe("cat", backend="openai")
    assert calls == ["openai:claude-x:90"]
    assert result.model == "claude-x"


# --- offline features never touch assist -------------------------------------------------------

OFFLINE_SCRIPT = """
import builtins
import sys

real_import = builtins.__import__


def guarded(name, *args, **kwargs):
    if name == "anthropic" or name.startswith("anthropic."):
        raise ImportError("anthropic is not installed (blocked by test)")
    return real_import(name, *args, **kwargs)


builtins.__import__ = guarded

from clipforage import recall, rights, service
from clipforage.models import Asset, Kind, SearchFilters
from clipforage.providers import registry

registry.load_builtins = lambda: None


class Fake:
    id = "fake"
    kinds = frozenset({Kind.AUDIO_MUSIC, Kind.AUDIO_SFX})
    requires_key = False

    def search(self, query, kind, filters):
        return [
            Asset(provider="fake", id=f"{kind.value}-{query}", kind=kind, title=query,
                  source_url="https://example.com", license="unknown",
                  attribution_text="x", commercial_ok=None)
        ]

    def resolve(self, asset):
        raise NotImplementedError


registry.register(Fake())
outcome = service.search_expanded(
    "undertale bgm", SearchFilters(kind=Kind.AUDIO_MUSIC), limit=3, use_cache=False
)
assert outcome.assets and outcome.related, outcome
assert recall.expand_query("dog in burning room") == ["this is fine"]
assert rights.classify(outcome.assets[0]).level == "copyrighted"  # soundtrack rule
loaded = sorted(m for m in sys.modules if m.startswith("clipforage.assist") or m == "anthropic")
assert not loaded, loaded
print("ok")
"""


def test_search_recall_rights_never_import_assist_or_anthropic() -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", OFFLINE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
