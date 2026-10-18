# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Reworked the README for non-coders with a pixel-art banner, step-by-step setup,
  first-download walkthrough and troubleshooting.
- Consolidated provider rules into the contributor docs and removed dated research write-ups.
- Added an optional Ko-fi support link.

### Removed

- Deprecated command aliases, environment-variable fallbacks, directory and browser-storage
  migrations, alternate request headers and obsolete provider plugin entry points.

## [0.1.0] - Unreleased

First public release.

### Added

- Unified search across YouTube (yt-dlp) and free stock / public-domain sources: Pexels,
  Pixabay, Openverse, Wikimedia Commons, Freesound, Internet Archive, Google Fonts, plus the
  scraped allowlist Mixkit and LottieFiles. Third-party providers load as plugins.
- License, attribution, source URL and a rights level on every result; `--cleared-only`.
- Downloads as mp4 / mp3 with a license sidecar, HTTP and YouTube clip ranges, and a quality
  cap; YouTube audio search for music and sound effects.
- Style ranking (`--style meme|green_screen|cinematic`) and a `--shorts` filter.
- Offline fuzzy meme recall with query expansion, plus Know Your Meme entry lookup.
- Phrase search in YouTube captions and Internet Archive subtitles.
- Chroma-key preview in the web UI and `keyout` export to a transparent `.key.webm`.
- Safe search, on by default.
- Local web UI (`clipforage serve`) with previews, downloads drawer, history and settings.
- DaVinci Resolve media-pool import (`resolve-import`).
- Optional LLM search assist (`describe`), off by default.
- `setup` / `doctor` for ffmpeg and Deno.

### Fixed

- `--cleared-only` is passed to providers instead of only filtering afterwards: Openverse asks
  for CC0 / PDM / CC BY / CC BY-SA and Internet Archive audio for non-NC licenses, so their
  results are no longer a page of non-commercial hits that the filter then drops entirely.
  A cleared-only audio search with no results retries once without generic words such as
  "background music" and says so.
- `--shorts` enforces the 60 s cap on every provider (videos of unknown length are dropped),
  puts YouTube results first and sorts only the other providers' results portrait first,
  because YouTube orientation is never known; off-topic portrait stock clips no longer take the
  top slots of meme searches. `--style cinematic` keeps portrait first across providers.
- With Style = Any, Internet Archive results rank after all other providers instead of taking
  the top slots.
- `get <ref> --mp3` (and web `mp3` downloads of audio results) is a no-op when the source is
  already a direct mp3, such as Freesound; other direct files still reject `--mp3`.
- `search` no longer prints `warning: pexels: no API key` on every run; a provider without a
  key is skipped with a one-line hint, once per provider.
- Pixabay result titles are the title-cased tags (`Rain, Window, Drops`) instead of
  `Pixabay video NNN: tags...`; the id stays in `extra["pixabay_id"]`.
- The web UI search box no longer comes up prefilled with the query from a previous session;
  other saved search settings are still restored.
- The web UI History Actions column no longer overflows at narrow widths (about 800-1000 px):
  action buttons wrap and long file names break.
- `clipforage links` marks sites that need an account, e.g. `Instagram Reels (account
  required)`, matching the web UI tooltip.
- Web UI result cards for assets already in the library show the draggable "Saved" handle after
  a re-search or reload, not only right after a download in the same session.
- The related audio section drops long-form noise: titles such as DJ mixes, mixtapes,
  audiobooks, podcasts or "10 hours", and items over 10 min (related music) or 60 s (related
  SFX). In audio searches of two or more words, results with such a noise title, or with no
  query word in the title and an unknown or over-long duration, rank after the rest.

### Known limitations

- Cleared music is thin: `--kind audio_music --cleared-only` asks Openverse and Internet
  Archive for cleared licenses (mostly CC samples, Jamendo and netlabel tracks), and a relaxed
  retry runs when the full query finds nothing; large libraries such as Uppbeat, YouTube Audio
  Library and Pixabay Music remain link-outs only.
- The chroma preview does not work on YouTube results (iframe); `keyout` is reachable from
  History only.
- A plain `ffprobe` reports `yuv420p` for `.key.webm` output; the alpha is present
  (`ffprobe -c:v libvpx-vp9` shows `yuva420p`).
- Dropping onto an editor timeline and importing `.key.webm` into editors are not verified.

Full list: [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).

[0.1.0]: https://github.com/Ry0x7/ClipForage-source
