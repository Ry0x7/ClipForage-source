# ClipForage: Research

Last reviewed: 2026-10-03

## Problem & user

Users are video editors working in three styles, weighted equally: meme (green-screen edits), cinematic, and vlog. All three hunt across a dozen sites for B-roll, overlays, LUTs, fonts, GIFs/stickers, SFX, and music, each site with its own license. ClipForage is one search box over these sources: license on every result, assets downloaded (mp4/mp3/etc.) into a local library folder the NLE imports from.

## How editors find assets today

- Meme edits: YouTube searches like "green screen meme", "green screen memes for editing", "chroma key meme", "[meme name] green screen"; aggregators memescreens.com, memes-download.com/category/green-screen, Know Your Meme (https://knowyourmeme.com/memes/green-screen-edits). Meme clips are almost always copyrighted third-party footage, so the tool labels them `unknown`.
- Cinematic edits: stock B-roll (Pexels, Pixabay, Coverr, Mixkit, Videvo), PD film on Internet Archive, "free LUT pack" and light-leak/film-grain overlay downloads, display fonts.
- Vlog edits: B-roll in landscape and portrait (shorts), background music, transitions, animated text/overlays, fonts.
- Every style: a separate downloader, then license guesswork.

## Source landscape

### Video

| Source | API | Auth | Free limits | License & commercial | Verdict |
|---|---|---|---|---|---|
| YouTube Data API v3 | Yes | Key | 10k units/day, search.list = 100 units | Policy forbids scraping (https://developers.google.com/youtube/terms/developer-policies) | Avoid (quota too small) |
| YouTube via yt-dlp `ytsearchN:` | No (extractor) | None | Bot detection "Sign in to confirm you're not a bot" (https://github.com/yt-dlp/yt-dlp/issues/12475) | Per video, usually unknown | Tier-1 (user's machine only) |
| Pexels | Yes | Key | 200/hr, 20k/mo | Free use; branding link required (https://www.pexels.com/api/documentation/) | Tier-1 |
| Pixabay | Yes | Key | 100 per 60s; must cache 24h; no mass download | Free use (https://pixabay.com/api/docs/) | Tier-1 |
| Coverr | Yes | Key | 50/hr free | Attribution required (https://coverr.co/developers) | BYO-key optional |
| Internet Archive (incl. PD film, Prelinger) | advancedsearch / scrape | None | Polite use | Per-item license (https://archive.org/developers/) | Tier-1 |
| Mixkit (B-roll, overlays, transitions) | No (server-rendered list pages) | - | - | No redistribution, no bulk downloading (https://mixkit.co/terms/) | Scraped, allowlist (2026-10-03; see below) |
| Videvo (B-roll) | No | - | - | Attribution license (https://help.videvo.net/article/28-videvo-attribution-license) | Link-out |

Overlays (light leaks, film grain, transitions) are available as ordinary video results from Pixabay and Pexels; most "free overlay pack" sites have no API and become link-outs.

### Image / GIF / Sticker

| Source | API | Auth | Free limits | License & commercial | Verdict |
|---|---|---|---|---|---|
| Klipy | Yes | Key (free prod key after form) | Free tier | "Powered by KLIPY" attribution (https://klipy.com/api-overview) | Tier-1 |
| Giphy | Yes | Key | Beta 100/hr; production paid since 2025-03 (https://support.giphy.com/hc/en-us/articles/10389869671322) | Giphy terms | BYO-key optional |
| Tenor | Discontinued | - | New keys frozen 2026-01-13, API discontinued 2026-06-30 (https://9to5google.com/2026/06/30/google-tenor-api-gif-updates/) | - | Avoid |
| Openverse | Yes | Optional OAuth | Anon 100/day, 5/hr; authed 10k/day (https://api.openverse.org/v1/) | CC / PD per item | Tier-1 |
| Wikimedia Commons | Yes | Descriptive User-Agent | 500/hr; non-compliant UA drops to 10/min (https://api.wikimedia.org/wiki/Documentation/Getting_started/Rate_limits) | CC / PD per file | Tier-1 |
| Imgur | Yes | Client ID | ~12.5k/day | Commercial use needs approval | Avoid |
| Reddit | Yes | OAuth, manual approval | 100 QPM (https://www.socialcrawl.dev/blog/reddit-data-api-2026) | Non-commercial | Avoid |
| Imgflip | Yes | Account | search_memes paywalled $9.99/mo (https://imgflip.com/api) | Templates, unclear | Avoid |
| Unsplash | Yes | Key | 50/hr demo; hotlink-only (https://unsplash.com/documentation) | Unsplash license | Avoid (no local download) |
| Freepik / Flaticon | Yes | Key | Paid (https://www.freepik.com/api/pricing) | Paid tiers | Avoid |
| Telegram stickers | Bot API `getStickerSet` | Bot token | - | Per pack, unknown (https://core.telegram.org/api/stickers) | BYO-key optional (later) |
| Signal stickers | Community index (signalstickers.org) | None | - | Unknown | Link-out |
| LottieFiles (animated overlays) | No documented public API: GraphQL API docs marked "Coming soon"; only an OAuth MCP server for account data (https://docs.lottiefiles.com/en/platform); the undocumented graphql.lottiefiles.com endpoint is used since 2026-10-03 | - | None published | Simple License (https://lottiefiles.com/page/license); not exposed per item | Scraped, allowlist (see below) |

### Audio

| Source | API | Auth | Free limits | License & commercial | Verdict |
|---|---|---|---|---|---|
| Freesound | Yes | Key; OAuth2 for originals | 60/min, 2k/day | API non-commercial by default; per-sound CC (https://freesound.org/docs/api/terms_of_use.html) | Tier-1 (CC0 filter default) |
| Openverse audio (music + SFX) | Yes | Optional | As above | CC per item; sources are Jamendo, Wikimedia audio, Freesound (https://api.openverse.org/v1/audio/stats/); Jamendo tracks are often CC BY-NC, so surface the per-result license and honor `--commercial-only` here too | Tier-1 |
| Free Music Archive | No own API; not an Openverse audio source (sources are Jamendo, Wikimedia Commons, Freesound: https://api.openverse.org/v1/audio/stats/) | - | - | CC per track (https://freemusicarchive.org) | Link-out |
| Internet Archive audio | Yes | None | Polite use | Per-item | Tier-1 |
| Jamendo | Yes | Key | ~35k/mo | Non-commercial, no caching (https://devportal.jamendo.com/api_terms_of_use) | Avoid |
| ccMixter | `/api/query` | None | - | CC per track (https://ccmixter.org/api/query) | BYO-key optional (later) |
| Zapsplat | No | - | - | Bans bots (https://www.zapsplat.com/license-type/standard-license/) | Link-out |
| Uppbeat | No | - | 3 downloads/mo free | Uppbeat license | Link-out |
| YouTube Audio Library | No | - | - | Per track | Link-out |
| Pixabay music | Not in API | - | - | Pixabay license | Link-out |

Jamendo is the richest vlog-music API, but its non-commercial terms conflict with monetized videos, hence Avoid.

### Fonts, LUTs, templates

| Source | API | Auth | Free limits | License & commercial | Verdict |
|---|---|---|---|---|---|
| Google Fonts Developer API | Yes, `GET https://www.googleapis.com/webfonts/v1/webfonts?key=` (https://developers.google.com/fonts/docs/developer_api) | Key (keyless calls return 403) | Free; no quota stated in the docs | OFL 1.1 (most), Apache 2, Ubuntu Font License 1.0 (https://github.com/google/fonts#license), commercial OK | Tier-1 |
| Font Squirrel | Yes, keyless JSON (`/api/classifications`, `/api/fontlist/{all\|name}`, `/api/familyinfo/{family}`), still live (https://www.fontsquirrel.com/blog/2010/12/the-font-squirrel-api) | None | None published | Commercial-use fonts, per font | Link-out |
| DaFont | No | - | - | License per font, often personal-use only | Avoid |
| RocketStock 35 Free LUTs (now hosted on the Pond5 blog) | No | - | - | Free .cube zip; no license terms on the page (https://blog.pond5.com/78810-35-free-luts-for-color-grading-videos/) | Link-out |
| IWLTBAP free LUTs ("Free stuff": Aspen, Humble, Sedona, etc.) | No | - | - | Free / pay-what-you-want; "do not resell ... do not share it for free" (https://iwltbap.com/) | Link-out |
| Lutify.me free LUTs (5 LUTs, free fylm.ai Lite account required) | No | - | - | Terms in https://lutify.me/license/ (not reviewed) (https://lutify.me/free-luts/) | Link-out |
| Mixkit templates | No | - | - | Mixkit license (https://mixkit.co/terms/) | Link-out |

There is no clean API ecosystem for LUTs; a curated link-out directory is the honest option.

## Scraping feasibility (2026-10-03)

The project decided (2026-10-03) to allow throttled scraping for a short allowlist (rules in
`.claude/rules/providers.md`, mechanics in [ARCHITECTURE.md](ARCHITECTURE.md), "Scraping layer").
The table below records why each candidate was or was not added, from the research done on
2026-10-03. Where a robots.txt result re-checked on 2026-10-03 with Python's
`urllib.robotparser` (`User-agent: *`) differs from the initial probe, both are given
("re-check: ...").

| Site | Search URL | Render | Download without login | robots.txt (`*`) | ToS note | Verdict |
|---|---|---|---|---|---|---|
| Mixkit video | `https://mixkit.co/free-stock-video/<slug>/` (slug = hyphenated query; `?q=` is ignored) | Server-rendered HTML (Rails / Stimulus); JSON-LD states the license tier for the first items only | Yes: `assets.mixkit.co/videos/<id>/<id>-1080.mp4`, newer items via the download modal | `Allow: /` (CCBot blocked) | Mixkit License; ToS bans mass downloading, scraping the whole library and building a "competitive product" (https://mixkit.co/terms/) | **Scrapable (HTML)**: `mixkit`, one list page per search |
| Mixkit SFX | `https://mixkit.co/free-sound-effects/<slug>/` | Server-rendered HTML | Yes, via the `/free-sound-effects/download/<id>/` modal | Same as Mixkit video | Same as Mixkit video | **Scrapable (HTML)**: `mixkit` |
| LottieFiles | HTML search is behind Cloudflare (403); `POST https://graphql.lottiefiles.com/` `searchPublicAnimations` works unauthenticated (undocumented) | GraphQL JSON: name, slug, url, lottieUrl, jsonUrl, imageUrl, videoUrl | Yes, from `assets-v2.lottiefiles.com` | Main host unreadable (403); 404 on the GraphQL host (allow) | Lottie Simple License (https://lottiefiles.com/page/license); endpoint undocumented; no per-item license field in the schema | **Scrapable (JSON), fragile**: `lottiefiles`, license labelled unverified |
| Know Your Meme | `https://knowyourmeme.com/search?q=<q>&context=entries` (server HTML, but robots-disallowed); entry pages `https://knowyourmeme.com/memes/<slug>` | Server-rendered: title `h1`, aliases in `<strong>` in the About paragraph, Type / Year / Origin `dl`; no autocomplete JSON | n/a (names only) | `/search` disallowed; `/memes/<slug>` allowed | Third-party scrapers report bans | **Scrapable (HTML), entry-page lookups only**: `recall_online`, never `/search` |
| Zapsplat | `https://www.zapsplat.com/?s=<q>&post_type=music` | Server-rendered HTML (WordPress); full-length preview mp3 and duration in data attributes | No (login, admin-ajax nonce) | Permissive | Standard License, free with attribution; website terms: "Automated access (bots, scrapers, scripts) is prohibited" (https://www.zapsplat.com/website-terms-of-use/) | **Link-out only**: login required, explicit bot ban |
| Uppbeat | `https://uppbeat.io/search/<q>` | Client-rendered Next.js behind the Vercel Security Checkpoint (429 on every request, robots.txt included) | No (account; 3 downloads/month free) | 429 (unreadable) | User Agreement forbids scripts / automations to download (https://uppbeat.io/user-agreement) | **Not feasible**: link-out |
| YouTube Audio Library | `https://www.youtube.com/audiolibrary` redirects to `studio.youtube.com/channel/UC/music` | Client-side SPA | No (Google login) | re-check: `/audiolibrary` allowed on www.youtube.com | YouTube ToS forbids automated access | **Not feasible**: link-out |
| Pixabay Music | `https://pixabay.com/music/search/<q>/` (not in the Pixabay API) | List page is server HTML (title, artist, duration); detail pages behind Cloudflare (403) | Unknown | Initial probe: behind Cloudflare 403; blocks `/api/`, download paths and AI user agents. Re-check: robots.txt returned 200 and `/music/search/...` is allowed for `*` | ToS forbids systematic mass downloads and automated queries (https://pixabay.com/service/terms/) | **Not feasible**: keep the official API for images / video; music stays link-out |
| Videvo / Freepik Videos | videvo.net redirects to Freepik; `https://www.freepik.com/search?format=search&type=video&query=<q>` | Cloudflare 403 | No (login; attribution required) | re-check: `/videos` allowed, `/search` disallowed | Mixed licenses; free clips need attribution | **Not feasible**: link-out |
| PlayPhrase | `https://www.playphrase.me/#/search?q=` | ClojureScript SPA with a bot-check token; `/api/v1/phrases/search` returns 403 without the token | - | re-check: robots.txt 404 | Site states it has no public or private API (https://www.playphrase.me/fair-use-policy.html); clips are copyrighted | **Not feasible**: link-out (quote lookup) |
| RocketStock free LUTs | None: a single Shutterstock blog post | Shutterstock answers 403 | - | Blocks AI user agents (re-check: the blog path is allowed for `*`) | Per-pack terms | **Not feasible, low value**: link-out |
| IWLTBAP free LUTs | None: about 7 free packs | Static pages | Zips via `/get-the-pack.php` | `/get-the-pack.php` disallowed (re-check: `/` on iwltbap.com allowed) | Terms forbid redistribution (https://luts.iwltbap.com/terms-of-sale.html) | **Not feasible**: link-out |

TikTok (`https://www.tiktok.com/`) and Instagram Reels (`https://www.instagram.com/`) were
added as browse-only link-outs on 2026-10-04 by decision, not after a scraping study: nearly
all of their content is copyrighted, and clipforage neither searches nor downloads from them.

Know Your Meme note: its robots.txt disallows `/search` for every user agent, so ClipForage never
queries it. Instead `recall_online.kym_lookup` guesses entry slugs (the query and up to two
offline recall names, slugified) and fetches `/memes/<slug>`, which robots.txt allows. For
descriptions that do not slugify to an entry, it also reads the confirmed-memes sitemap
(`/memes/confirmed/sitemap.xml`, declared in robots.txt and not disallowed). Probed 2026-10-03:
a single gzip urlset, ~3.7 MB decoded, ~35 s to the first byte, 23,369 URLs of which 16,229 are
`/memes/<slug>`; 2,292 more are `/sensitive/memes/<slug>` (for example
`woman-yelling-at-a-cat`, whose `/memes/` URL 301s there), and the rest are
`/memes/people|events|subcultures|sites|cultures/<slug>`. The sitemap is fetched at most once per
7 days and cached; up to 3 slugs sharing the most words with the query are fetched as extra
entry pages (`recall_online.fuzzy_slugs`). Only `/memes/<slug>` slugs are used.

Scraping is fragile and ToS-sensitive: any of these sites can change markup, endpoints, robots.txt
or terms without notice. ClipForage treats a failure as that provider's error and keeps searching.

## Download & processing tech

- yt-dlp latest release 2026.08.19 (https://github.com/yt-dlp/yt-dlp/releases); Python API: `extract_info(download=False)`, `progress_hooks`, `download_ranges`, postprocessors.
- Since 2025.11 yt-dlp needs a JS runtime for YouTube: Deno >= 2 recommended, Node >= 20 supported, selected via `--js-runtimes`, with `--remote-components` for the EJS solver (https://github.com/yt-dlp/yt-dlp/issues/14404, https://github.com/yt-dlp/ejs).
- PO tokens are bound to the video ID; the bgutil provider plugin generates them (https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide, https://github.com/Brainicism/bgutil-ytdlp-pot-provider).
- ffmpeg: imageio-ffmpeg can ship a binary (https://pypi.org/project/imageio-ffmpeg/); Windows builds from gyan.dev and BtbN.
- Node alternatives (yt-dlp-wrap, @distube/ytdl-core) are unmaintained; youtubei.js is fragile (https://github.com/LuanRT/YouTube.js).

```sh
# webm -> mp4
ffmpeg -i in.webm -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart out.mp4
# mp3
ffmpeg -i in.mp4 -vn -c:a libmp3lame -q:a 2 out.mp3
# trim (-c copy snaps to keyframes: a 2.0 s request gave 2.23 s; for frame-accurate trims re-encode with -c:v libx264 -c:a aac instead of -c copy)
ffmpeg -ss 10 -to 25 -i in.mp4 -c copy clip.mp4
# thumbnail
ffmpeg -ss 5 -i in.mp4 -frames:v 1 -vf scale=320:-1 thumb.jpg
# GIF
ffmpeg -i in.mp4 -vf "fps=12,scale=480:-1,palettegen" pal.png
ffmpeg -i in.mp4 -i pal.png -lavfi "fps=12,scale=480:-1[x];[x][1:v]paletteuse" out.gif
# chroma key -> transparent webm
ffmpeg -i in.mp4 -vf "colorkey=0x00FF00:0.3:0.1" -c:v libvpx-vp9 -pix_fmt yuva420p out.webm
```

Chroma key: `chromakey` works in YUV, `colorkey` in RGB (https://ffmpeg.org/ffmpeg-filters.html#colorkey).

## Existing systems compared

| Project | Stack | What to borrow |
|---|---|---|
| cobalt (https://github.com/imputnet/cobalt) | Node + Svelte, AGPL | Clear disclaimer; single-box UX |
| Stacher (https://stacher.io) | Electron, closed | Fetch yt-dlp/ffmpeg at runtime instead of bundling |
| Parabolic (https://github.com/NickvisionApps/Parabolic) | C# | Lawful-use disclaimer wording |
| Media Downloader (https://github.com/mhogomchungu/media-downloader) | Qt | Pluggable extensions (best plugin model); runtime binary download |
| Seal | Kotlin (Android) | yt-dlp format presets |
| yt-dlg | Python, stale since 2023 | Cautionary: GUI wrappers rot without maintenance |
| gallery-dl | Python | Image-site extractor patterns |
| Invidious / Piped | Proxy frontends | Avoid: instances collapsing (https://dev.to/selfhostingsh/invidious-vs-piped-4ijn) |

## Editor integration options

- Folder import / OS drag-drop: works everywhere. Default.
- Premiere Pro UXP panel: drag-drop JSON `{version, items:[{name, content_type, uri}]}` (https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/drag-and-drop/).
- DaVinci Resolve: `MediaPool.ImportMedia` via scripting (https://resolvedevdoc.readthedocs.io/en/latest/API_basic.html).
- Kdenlive: D-Bus (https://github.com/D-Ogi/kdenlive-api).
- CapCut: no API; folder import only.
- Desktop shell: Tauri 2 sidecar via `bundle.externalBin` (https://tauri.app/develop/sidecar/); watch NSIS reinstall bug #15134.

## Legal / ToS findings & red flags

- YouTube ToS forbids downloading except as authorized, and forbids automated access (https://www.youtube.com/static?template=terms). yt-dlp must run on the user's machine only, never hosted or proxied.
- youtube-dl was hit by an RIAA §1201 takedown on 2020-10-23 and reinstated by GitHub on 2020-11-16 (https://www.eff.org/deeplinks/2020/11/github-reinstates-youtube-dl-after-riaas-abuse-dmca). Lesson: no circumvention claims, no "YouTube downloader" marketing, no copyrighted test fixtures.
- Red flags: Pixabay forbids mass download; Zapsplat bans bots; Freesound and Jamendo APIs are non-commercial by default; CC BY-NC assets are unsafe for monetized videos; Unsplash is hotlink-only.
- Disclaimer models: Parabolic, cobalt, Media Downloader.

## Conclusions feeding the architecture

- D1: Python 3.12 + uv + Typer: yt-dlp is Python; Node wrappers unmaintained; Rust has no yt-dlp.
- D2: Three editing styles need many asset kinds (video, image, gif, sticker, audio_sfx, audio_music, font, lut, overlay, template); licenses vary per item, so every `Asset` carries license fields; plugins per Media Downloader.
- D3: Tier-1 = youtube, pexels, pixabay, openverse, wikimedia, klipy (deferred, ToS), freesound, internet_archive, google_fonts; no-API sites (LUT and overlay packs, Uppbeat, Zapsplat, ...) become link-outs. Updated 2026-10-03: Mixkit, LottieFiles and Know Your Meme entry pages are scraped under an allowlist (see "Scraping feasibility").
- D4: yt-dlp Python API + ffmpeg; ffmpeg and Deno fetched at runtime (Stacher pattern), not committed.
- D5: Useful APIs need keys that cannot be shipped: user-supplied config.
- D6: Pixabay's 24h rule and quotas require a SQLite TTL cache; sidecar JSON keeps the license with the file.
- D7: Disclaimer, license on every result, NC flagging, no hosting.
