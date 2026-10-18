# Known issues

Limitations and quirks of ClipForage as of 0.1.0. Design-level non-goals are in
[ARCHITECTURE.md section 7](ARCHITECTURE.md#7-non-goals--known-limitations); this page lists
concrete gaps and behaviours you may run into.

**Last verified:** 2026-10-06 (live smoke run, Windows 11, yt-dlp 2026.08.19).

Each issue starts with how much it is likely to affect you: **High** (a headline feature does
not do what you would expect), **Medium** (noticeable, with a workaround), **Low** (cosmetic or
rare).

## Not verified yet

- **Medium** Dropping a file dragged from the web UI onto an editor timeline (Premiere, Resolve,
  CapCut). The drag payload (`DownloadURL`, `text/uri-list`) and the file endpoint are verified;
  a real OS drop is not.
- **Medium** Importing transparent `.key.webm` files (VP9 alpha) into Premiere, Resolve and
  CapCut. Premiere may need a VP9-alpha-capable importer.
- **Low** The Resolve in-app script path (Workspace > Scripts); `resolve-import` itself is
  verified.
- **Low** The Premiere UXP skeleton in `spikes/premiere-uxp/` has never been loaded into
  Premiere.
- **Low** Pexels against its live API (fixture tests only), including its CDN redirects through
  the preview proxy.
- **Low** The `anthropic` assist backend against the live API, and the fall-through from a
  logged-out Claude Code CLI under `backend = auto`.
- **Low** `clipforage setup` downloads and cookies-from-browser against a real browser profile.
- **Low** The web UI in a real OS dark mode (only simulated) and in non-Chromium browsers.

## Search results and ranking

- **Medium** `--shorts` drops every video longer than 60 s or without a known duration, from
  every provider, so Internet Archive (no duration) never appears with it. YouTube
  orientation is unknown: flat search entries carry no width/height and no `/shorts/` URLs,
  and the "Type: Shorts" search filter returns nothing through yt-dlp. So YouTube results go
  first and only Pexels, Pixabay and Wikimedia results (orientation from width and height)
  are sorted portrait first after them (`cinematic` keeps portrait-first across providers).
  "sigma meme --shorts" (2026-10-06): 4 of 10 relevant (YouTube 4 of 4, stock 0 of 6) after
  the 60 s cap and before the YouTube-first ordering, up from 2 of 10 before the cap; not
  re-measured after the reorder.
- **Low** Related-kind sections in audio searches are noise-filtered by title keywords (DJ mix,
  audiobook, podcast, "10 hours", ...) and a duration cap (music 10 min, SFX 60 s), but items
  of unknown length and noise the keyword list misses still get through. The primary list is
  not keyword-filtered: Internet Archive mixtapes and radio episodes can still appear (after
  on-topic titles). Primaries are only demoted for queries of two or more words, and only when
  the title is noise or shares no query word and the length is unknown or over 10 min (music) /
  60 s (SFX): short off-topic filler keeps its slot, and an on-topic Internet Archive item with
  no duration and none of the query words in its title is demoted. `--no-related` drops the
  related section.
- **Low** Internet Archive results show no duration in CLI output.

## YouTube and yt-dlp

- **High** YouTube changes break yt-dlp regularly (bot checks, PO tokens). Update yt-dlp, then
  run `clipforage doctor`. Downloads may be throttled or blocked without cookies
  (`--cookies-from-browser`); a bgutil PO-token provider is not implemented.
- **Low** yt-dlp needs a JavaScript runtime (Deno or Node) since 2025.11; `clipforage setup`
  installs Deno.
- **Low** YouTube's best mp4 is often AV1, which Premiere and CapCut handle poorly; downloads
  prefer H.264 and fall back to any mp4.
- **Medium** Clip ranges on YouTube run through ffmpeg with no byte progress and can be slow
  (minutes for a few seconds of 1080p60).
- **Low** The under-4-minute search filter (`sp=EgQQARgB`) is an undocumented YouTube code and
  can stop working without notice.
- **Low** Safe search's YouTube `age_limit` check is inert: flat search results carry no
  `age_limit`, so YouTube relies on the shared blocklist only.
- **Low** Caption fetches for phrase search use plain HTTP without a PO token (works today).
- **Low** Sites like Vimeo often need a login; `get` suggests `--cookies-from-browser`.

## Downloads and clips

- **Medium** HTTP clip ranges (Internet Archive, stock sources) let ffmpeg seek over HTTPS.
  Badly interleaved old files can re-seek per packet and take minutes; such clips stop at the
  wall-clock limit (300 s default, `get --max-wall-s`, `0` = no limit) with a hint to download
  the full file and trim locally. Short clips may show no progress until 100%.
- **Low** Despill (preview and `keyout`) also desaturates genuinely green subjects.
- **Low** The chroma preview keys on the main thread (about 3 ms per 640x360 frame).
- **Medium** A plain `ffprobe file.key.webm` reports `yuv420p` (no alpha) because ffmpeg's
  built-in VP9 decoder ignores WebM alpha. The alpha is there:
  `ffprobe -c:v libvpx-vp9 file.key.webm` shows `yuva420p`. Inspect or convert with
  `ffmpeg -c:v libvpx-vp9 -i file.key.webm ...`. This does not mean `keyout` failed.

## Providers

- **Medium** There is no zero-config production-library music provider. `--kind audio_music
  --cleared-only` now asks Openverse and Internet Archive for cleared licenses only, but their
  catalogue is CC music (Jamendo, Wikimedia, IA netlabels), so niche or multi-word queries can
  still return few or no results; with no cleared hits an audio search retries once with
  generic words stripped ("phonk background music" -> "phonk"); cleared music sites such as
  Uppbeat, YouTube Audio Library and Pixabay Music are link-outs only (`clipforage links`).
- **Medium** Freesound API terms are non-commercial by default.
- **Low** Openverse downloads hosted on Flickr can time out.
- **Low** Wikimedia and Openverse previews use the original file, not a transcode, so they can
  be large or in a format the browser cannot play. Pixabay image assets and Google Fonts have no
  preview media.
- **Low** Pixabay `largeImageURL` links may expire; they are resolved close to download time.
- **Low** Internet Archive subtitles are mostly speech-recognition output with errors; phrase
  search finds IA films by title (`--within`).
- **Low** Phrases spread over four or more caption lines do not match.
- **Low** KLIPY is not implemented (its terms forbid caching and stored copies); Giphy is not
  supported.

## Scraped sources and Know Your Meme

- **Medium** Mixkit, LottieFiles and Know Your Meme depend on page markup or an undocumented
  endpoint and can break without notice; the search continues without them. See
  [ARCHITECTURE.md "Scraping layer"](ARCHITECTURE.md#scraping-layer).
- **Medium** LottieFiles exposes no per-item license (always "check license"); Mixkit states the
  video tier only for the first items on a list page.
- **Low** The Know Your Meme name index builds on first use (about 40 s) in the background. A
  plain CLI `search` exits before it finishes and prints a note; run
  `clipforage recall --online "<query>"` once to build it.
- **Low** Know Your Meme entries under nested category paths (`/memes/people/...`) and
  percent-encoded slugs are not indexed; entries filed as sensitive are hidden while safe search
  is on.

## Safe search and rights

- **Medium** Safe search is a blocklist and is best effort: explicit results can slip through
  and harmless ones can be hidden. Details:
  [ARCHITECTURE.md "Safe search pipeline"](ARCHITECTURE.md#safe-search-pipeline).
- **Medium** The rights level is a heuristic on the license string the source reports, not
  legal advice. Internet Archive `licenseurl` is set by the uploader, and live hits include
  commercial tracks tagged Public Domain Mark, so `--cleared-only` is no rights guarantee for
  IA music.

## Web UI and editors

- **Medium** The chroma-key preview does not appear for YouTube results, because they play in a
  `youtube-nocookie.com` iframe; it works only for video previews served through the local
  proxy. `Key out` is reachable only from the History tab, after a download.
- **Medium** Drag-out on a result card needs a previous download of that asset whose file is
  still in the library folder. It uses Chromium's `DownloadURL`, so it needs Chrome or Edge, and the drop target gets a copy
  of the file rather than a reference to the library file. Dropping onto an editor timeline has
  never been tested (see "Not verified yet").
- **Low** The YouTube embed (`youtube-nocookie.com`) is a direct external request; other
  previews go through the local proxy.
- **Low** The preview proxy's same-site redirect check uses a short two-label suffix list, not
  the full Public Suffix List.
- **Low** `serve --reload` cannot be combined with `--allow-remote`. `--allow-remote` has no
  auth.
- **Medium** `resolve-import` needs Resolve's external scripting set to Local, and
  `RESOLVE_SCRIPT_LIB` if Resolve is not in its default path ([EDITORS.md](EDITORS.md)). On
  Resolve 21.1 `ImportMedia` accepts plain path strings but not the documented dict form; the
  code tries both.

## Platform and configuration

- **Low** macOS ffmpeg is not installed by `setup`; it prints a `brew install ffmpeg` hint. The
  Windows/Linux ffmpeg is a rolling BtbN build, not a tagged release.
- **Low** On Windows a running `clipforage serve` locks the venv's `clipforage.exe`, so
  `uv sync` fails until it stops (use `uv run --no-sync` meanwhile).
- **Low** Writing `config.toml` from the web UI keeps comments, but a comment directly above a
  `[table]` header may end up before a newly added key of the previous table (cosmetic).
- **Low** Plugin providers run in-process as trusted code; a failed plugin is retried only after
  a restart.
