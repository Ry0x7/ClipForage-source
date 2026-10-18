# ClipForage Premiere UXP panel (spike)

**UNTESTED PROTOTYPE.** This panel has not been loaded into Premiere and is not a supported
feature. For everyday editing, use [folder import](../../docs/EDITORS.md#premiere-pro).

What it does: a search box that calls `GET http://127.0.0.1:8765/api/search` on a running
`clipforage serve`, then lists result titles with a license badge. Green means `commercial_ok`
is true, red means false, grey means unknown. It does not download or import anything.

## Requirements (from Adobe docs)

- Premiere Pro 25.6 or later, and UXP Developer Tool (UDT) 2.2 or later
  (https://developer.adobe.com/premiere-pro/uxp/plugins/).
- Developer Mode turned on in Premiere (Settings > Plugins > Enable developer mode, then restart)
  and in UDT on first launch
  (https://developer.adobe.com/premiere-pro/uxp/introduction/essentials/dev-tools/).

## Load it with UDT

1. Run `uv run clipforage serve` in the repo.
2. Open Premiere, then UDT.
3. In UDT, click **Add Plugin** and select `spikes/premiere-uxp/manifest.json`
   (https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/udt-deep-dive/plugin-management/).
4. Click **Load & Watch**. If you edit `manifest.json`, unload and reload the plugin.
5. Open the panel in Premiere and search.

## Things to record when you first run it

- Does the manifest load with `"domains": ["http://127.0.0.1:8765", ...]`? The docs only show
  https origins with no port. If it is rejected, try `"domains": "all"`, which is documented.
- Does `fetch` reach loopback on Windows? On macOS? (Adobe says macOS restricts `http://`.)
- Does the request arrive with an `Origin` header, and if so, what value? Does a POST that
  carries `X-Clipforage: 1` cause an OPTIONS preflight? (clipforage has no CORS middleware.)

## Notes

- No icons are included. The manifest docs show an `icons` array, but whether it is required for
  loading in UDT is not confirmed.
- `localFileSystem: "request"` is set because the drag-and-drop tutorial requires it. This
  search-only spike does not use it.
- The plugin `id` is a placeholder. A Creative Cloud Marketplace listing needs an ID from the
  Developer Distribution portal
  (https://developer.adobe.com/premiere-pro/uxp/plugins/distribution/package/).
