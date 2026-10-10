# Disclaimer

ClipForage is intended for lawful use only. You are solely responsible for how you use it,
including complying with copyright law in your jurisdiction and with the terms of service of every
source it searches or downloads from (for example YouTube, Pexels, Pixabay, Openverse, Wikimedia
Commons, Freesound, the Internet Archive, Google Fonts, Mixkit, LottieFiles, and Know Your
Meme). The tool ships no credentials, cookies, or third-party binaries, and it does not host or
proxy any content: all searches and downloads run locally on your machine, using API keys and
tools you supply yourself.

Meme clips and other videos found through YouTube search are typically third-party copyrighted
works. A result's license field is informational and may be "unknown"; determining whether you may
use, modify, or publish a given file (video, audio, image, font, LUT, overlay, or template), and
obtaining any required permission or attribution, is your
responsibility. ClipForage is not affiliated with, endorsed by, or sponsored by YouTube,
Google, or any other provider. The software is provided "as is", without warranty of any kind,
under the MIT License (see [LICENSE](LICENSE)).

Phrase search only reads the captions and subtitle files of sources you can already access
yourself (YouTube and the Internet Archive), on your machine, and keeps the text in your local
cache. ClipForage does not host, collect, or redistribute any film or quote corpus; finding a
phrase in a video does not change that video's license.

The rights level shown on each result (for example "Free to use" or "Copyrighted – Content ID
likely") is an automatic guess from the license the source reports, not legal advice.

A few sources without a public API (Mixkit, LottieFiles and Know Your Meme entry pages) are read
by fetching their pages or endpoints from your machine: at most one request per second per site,
only paths their robots.txt allows, with an honest User-Agent, and nothing is crawled. Those
sites' terms still apply to you, and it is your responsibility to check that your use complies
with them; you can turn this off with `[scrape] enabled = false`. These sources can change or
stop working at any time.

Safe search is on by default and filters explicit results on a best-effort basis; it can miss
explicit content and can hide harmless results.

The optional AI search assist is off by default. Only when you enable it and submit a
description is that text (with the asset kind and style you chose) sent to the third-party model
provider you picked (Anthropic, through Claude Code or its API, or OpenAI), under that provider's
terms and at your own cost. ClipForage does not send search queries or files to these services.
