from collections.abc import Iterator

import pytest

from clipforage import __version__, http


@pytest.fixture(autouse=True)
def _reset_shared_client() -> Iterator[None]:
    http.reset_client()
    yield
    http.reset_client()


def test_user_agent_has_version_and_repo_url() -> None:
    assert http.USER_AGENT == (
        f"clipforage/{__version__} (+https://github.com/Ry0x7/ClipForage-source)"
    )


def test_make_client_sets_user_agent_and_defaults() -> None:
    with http.make_client() as client:
        assert client.headers["User-Agent"] == http.USER_AGENT
        assert client.follow_redirects is True
        assert client.timeout.read == 20.0


def test_make_client_merges_extra_headers_and_timeout() -> None:
    with http.make_client(timeout=5.0, headers={"Authorization": "key"}) as client:
        assert client.headers["User-Agent"] == http.USER_AGENT
        assert client.headers["Authorization"] == "key"
        assert client.timeout.connect == 5.0


def test_get_client_is_shared() -> None:
    assert http.get_client() is http.get_client()


def test_reset_client_yields_fresh_client() -> None:
    first = http.get_client()
    http.reset_client()
    assert first.is_closed
    second = http.get_client()
    assert second is not first
    assert not second.is_closed
