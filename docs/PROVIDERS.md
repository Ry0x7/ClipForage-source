# Provider rules

- Implement the `Provider` Protocol from `src/clipforage/providers/base.py`: `id`, `kinds`,
  `search(query, kind, filters) -> list[Asset]`, `resolve(asset) -> DownloadPlan`.
- `kinds` must be a subset of the asset-kind enum in `models.py`:
  `video | image | gif | sticker | audio_sfx | audio_music | font | lut | overlay | template`.
  Do not invent new kind strings in a provider; add them to the enum first.
- Every returned `Asset` must set `license`, `attribution_text`, `source_url`, and `commercial_ok`.
  Use the string `"unknown"` for an unknown license, never `None`.
- Use the shared `httpx.Client` from the context; do not create your own client. The client sends
  the project User-Agent `clipforage/<version> (+<repo url>)`.
- Respect the provider's TTL constant for the search cache (Pixabay: 24h minimum).
- Scraping (sites without a public API) is allowed ONLY for this allowlist: Mixkit list pages
  (`mixkit`), the LottieFiles GraphQL endpoint (`lottiefiles`), Know Your Meme entry pages
  (`recall_online`, never `/search`, which robots.txt disallows) and its confirmed-memes
  sitemap (`/memes/confirmed/sitemap.xml`), fetched at most once per 7 days and cached, for
  fuzzy slug matching. Its `/sensitive/memes/` slugs are fuzzy candidates only with safe search
  off. Searches and the web UI never wait on the sitemap (one background daemon fetch on a
  miss); only `recall --online` fetches it synchronously. With 2+ query key words a fuzzy
  candidate shares 2 of them or has a `SequenceMatcher` ratio >= 0.8. Everything else stays
  link-out only (`linkout.py`). Adding a site to the allowlist is a decision, not a refactor.
- Every scraping request goes through `src/clipforage/scrape.py` (`fetch_html` / `request`):
  robots.txt checked, per-host throttle (>= 1 s), honest project User-Agent. Set
  `scrapes = True` on the provider (skipped while `[scrape] enabled = false`) and
  `SEARCH_TTL_S = 86400` (24h cache).
- Never paginate or bulk-fetch: one list page / one query per search, no detail-page fan-out
  during search (fetch extra pages only in `resolve()`, for the single asset being downloaded).
  The single exception is the Know Your Meme confirmed-memes sitemap above: one bulk document
  (child sitemaps only if it becomes an index, at most 10), cached 7 days, capped at 50,000
  slugs / 20 MB. It is never used to fetch entry pages in bulk (at most 3 fuzzy entries per
  lookup).
- Document the scraper as fragile in its module docstring (markup/endpoint may change; it then
  raises `ProviderError` and the search continues without it).
- On failure raise `ProviderError`; never let any other exception escape `search`.
- Add recorded fixture JSON under `tests/fixtures/<id>/` and a `respx` test that uses it.
- Exception: yt-dlp-backed providers use the yt-dlp Python API instead of the shared httpx client
  and test via an injected `ydl_factory`.
- Register the provider in `src/clipforage/providers/registry.py`.
- Document the key name `CLIPFORAGE_<ID>_KEY` in the provider module docstring,
  or state "no key required".
