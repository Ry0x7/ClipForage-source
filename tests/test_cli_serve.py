"""`clipforage serve`: host guard, browser opening, uvicorn args (uvicorn.run is patched)."""

import webbrowser
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from clipforage.cli import app

runner = CliRunner()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    recorded: dict[str, list[Any]] = {"run": [], "open": []}

    def fake_run(target: Any, **kwargs: Any) -> None:
        recorded["run"].append((target, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(webbrowser, "open", lambda url: recorded["open"].append(url))
    return recorded


def test_serve_no_open_runs_uvicorn_on_localhost(calls: dict[str, list[Any]]) -> None:
    result = runner.invoke(app, ["serve", "--no-open", "--port", "9123"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:9123/" in result.output
    assert calls["open"] == []
    [(target, kwargs)] = calls["run"]
    assert hasattr(target, "router")  # a FastAPI app instance, not an import string
    assert kwargs == {"host": "127.0.0.1", "port": 9123, "log_level": "info"}


def test_serve_opens_browser_by_default(calls: dict[str, list[Any]]) -> None:
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    assert calls["open"] == ["http://127.0.0.1:8765/"]
    assert calls["run"][0][1]["port"] == 8765


def test_serve_reload_uses_factory_import_string(calls: dict[str, list[Any]]) -> None:
    result = runner.invoke(app, ["serve", "--no-open", "--reload"])
    assert result.exit_code == 0, result.output
    [(target, kwargs)] = calls["run"]
    assert target == "clipforage.web.app:create_app"
    assert kwargs["factory"] is True
    assert kwargs["reload"] is True


def test_serve_refuses_remote_host(calls: dict[str, list[Any]]) -> None:
    result = runner.invoke(app, ["serve", "--no-open", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "--allow-remote" in result.output
    assert calls["run"] == []


def test_serve_allow_remote_warns(calls: dict[str, list[Any]]) -> None:
    result = runner.invoke(app, ["serve", "--no-open", "--host", "0.0.0.0", "--allow-remote"])
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    assert "NO authentication" in result.output
    [(target, kwargs)] = calls["run"]
    assert kwargs["host"] == "0.0.0.0"
    # Host check disabled: any Host header is accepted.
    assert TestClient(target).get("/", headers={"Host": "evil.example"}).status_code == 200


def test_serve_default_keeps_host_check(calls: dict[str, list[Any]]) -> None:
    result = runner.invoke(app, ["serve", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output
    [(target, _)] = calls["run"]
    assert TestClient(target).get("/", headers={"Host": "evil.example"}).status_code == 400
    assert TestClient(target).get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_serve_allow_remote_on_loopback_still_disables_host_check(
    calls: dict[str, list[Any]],
) -> None:
    result = runner.invoke(app, ["serve", "--no-open", "--allow-remote"])
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    [(target, _)] = calls["run"]
    assert TestClient(target).get("/", headers={"Host": "evil.example"}).status_code == 200


def test_serve_reload_with_allow_remote_refused(calls: dict[str, list[Any]]) -> None:
    result = runner.invoke(
        app, ["serve", "--no-open", "--reload", "--allow-remote", "--host", "0.0.0.0"]
    )
    assert result.exit_code == 1
    assert "use --reload without --allow-remote" in result.output
    assert calls["run"] == []
