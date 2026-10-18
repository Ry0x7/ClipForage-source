# Contributing

Issues and pull requests are welcome. ClipForage is a small Python project; this page covers
setup, conventions and how to add a source.

## Dev setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
git clone https://github.com/Ry0x7/ClipForage-source.git
cd ClipForage-source
uv sync                    # installs deps + the dev group into .venv
uv run pytest -x           # offline test suite (live tests are deselected)
uv run pytest -m live      # optional: real network calls, keys as CLIPFORAGE_<ID>_KEY env vars
uv run ruff check .        # lint
uv run ruff format .       # format (line length 100)
uv run clipforage doctor   # checks ffmpeg, Deno, yt-dlp
```

CI (`.github/workflows/ci.yml`) runs ruff and pytest on Ubuntu and Windows.

## Conventions

- Type hints on every `def`, including return types (`-> None`).
- HTTP through `httpx` only, never `requests`.
- No live network in tests. Mock HTTP with `respx` using recorded fixtures under
  `tests/fixtures/<provider>/`. Tests that need the network are marked `@pytest.mark.live`.
- One provider failing must not fail a search: raise `ProviderError`, the search reports it and
  continues with the other providers.
- Every `Asset` carries `license`, `attribution_text`, `source_url` and `commercial_ok`
  (`"unknown"` for an unknown license, never `None`).
- Pydantic models live in `src/clipforage/models.py`.
- Windows-first: build paths with `pathlib.Path`, never hardcoded `/` or `\` separators.
- Never commit API keys, cookies, ffmpeg/Deno binaries, `.venv/` or `cache.db`.

## Branches and commits

- One branch per topic (`feat/<topic>`, `fix/<topic>`, `docs/<topic>`); no feature work
  directly on `main`.
- [Conventional commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`,
  `test:`, `chore:`, `refactor:`.
- Bump yt-dlp in its own commit and run the live smoke tests (`uv run pytest -m live`).

## Providers

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (sections 3 and 5) and
[docs/PROVIDERS.md](docs/PROVIDERS.md) before touching providers or download
code. Scraping is allowed only for the allowlist in that rules file; adding a site to it is a
project decision, not a refactor. Every other site without an API is a link-out (`linkout.py`).

### Adding a provider in 5 steps

1. Create `src/clipforage/providers/<id>.py` implementing the `Provider` Protocol from
   `providers/base.py`: `id`, `kinds`, `search(query, kind, filters) -> list[Asset]` and
   `resolve(asset) -> DownloadPlan`. Use the shared `httpx` client.
2. Map the source's license to the four `Asset` license fields, and check that `rights.py`
   classifies it correctly (add a rule if needed).
3. Set the search-cache TTL the source's terms require, and `cache_empty_results` if empty
   results should be cached. Document the key name `CLIPFORAGE_<ID>_KEY` (or "no key required")
   in the module docstring.
4. Register it in `load_builtins()` in `src/clipforage/providers/registry.py`.
5. Record fixture JSON under `tests/fixtures/<id>/`, add a `respx` test, and add a row to the
   Providers table in `README.md`.

Third-party providers can also ship as a separate package through the `clipforage.providers`
entry point group (see "Provider plugins" in docs/ARCHITECTURE.md).
