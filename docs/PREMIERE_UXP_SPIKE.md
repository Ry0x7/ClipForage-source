# Premiere Pro UXP panel spike

Status: research spike.
Date: 2026-10-01. Skeleton: [`spikes/premiere-uxp/`](../spikes/premiere-uxp/) (untested; never
loaded into Premiere).

**Decision: GO-LATER.** See section 6.

Every UXP claim below links to the Adobe page it came from; all of them were fetched while this
was written. Anything not confirmed in the docs is marked **(unconfirmed)**.

## 1. What a Premiere UXP panel can do today

### Versions

- UXP in Premiere went to public beta in Premiere 25.2.0 (2024-12-04) and was officially released
  in 25.6.0 ([changelog](https://developer.adobe.com/premiere-pro/uxp/changelog/)).
- Building a plugin needs Premiere 25.6 or later, UXP Developer Tool (UDT) 2.2 or later, and
  "Enable developer mode" turned on under Settings > Plugins, followed by a restart
  ([plugins](https://developer.adobe.com/premiere-pro/uxp/plugins/)).
- **Drag and drop from a third-party panel needs Premiere Pro 27.0.0 or later**
  ([drag-and-drop tutorial](https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/drag-and-drop/)).
  The changelog does not list 27.0 yet. Whether 27.0 is released, beta-only, or unreleased right
  now is **(unconfirmed)**: helpx release notes returned 403.
- `Project.importFiles` and `Project.getActiveProject` are marked "Min Version: 25.6"
  ([Project class](https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/project/)).

### Manifest v5

From the [manifest page](https://developer.adobe.com/premiere-pro/uxp/plugins/concepts/manifest/):

- Fields: `manifestVersion: 5`, `id`, `name`, `version` (`major.minor.patch`), `main`
  (`index.html`), `host: {app: "premierepro", minVersion}`, `entrypoints` (type `panel`, `id`,
  `label.default`), `icons`, `requiredPermissions`.
- `requiredPermissions.network.domains` is either `"all"` or an array of origins such as
  `["https://example.com", "https://*.adobe.com/"]`.
- `requiredPermissions.localFileSystem` is `"plugin"`, `"request"` or `"fullAccess"`.

From the inter-plugin-comm tutorial example manifest
(https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/inter-plugin-comm, read through
context7, not WebFetch): optional panel sizing keys (`minimumSize`, `preferredDockedSize`, and so
on) and `host.minVersion: "25.6.0"`.

**(unconfirmed)** Whether an http origin with an explicit port (`"http://127.0.0.1:8765"`) is
accepted in `network.domains`. Every example in the docs is https with no port. If it is rejected,
the fallback is `"domains": "all"`, which is documented.

### Network: can the panel `fetch` 127.0.0.1?

From the [network recipe](https://developer.adobe.com/premiere-pro/uxp/resources/recipes/network/):

- `fetch()`, `XMLHttpRequest` and WebSocket clients are available. Plugins "cannot host or accept
  incoming connections".
- Requests to domains missing from `network.domains` are blocked.
- "All APIs support both HTTP and HTTPS, but macOS restricts `http://` for security reasons."
- In troubleshooting it says "Ensure your server allows requests from UXP (check
  Access-Control-Allow-Origin)", which suggests CORS can apply.

**(unconfirmed)**:
- The docs never mention loopback, localhost or 127.0.0.1.
- Whether macOS's `http://` restriction also applies to `http://127.0.0.1` (this is the biggest
  open question for macOS).
- What `Origin` header UXP sends, if any.
- Whether a custom request header (our `X-Clipforage: 1`) triggers a CORS preflight.

### Getting media into the project

**Option A, programmatic import (25.6+).**
`importFiles(filePaths: string[], suppressUI: boolean, targetBin: ProjectItem, asNumberedStills: boolean): Promise<boolean>`
on a `Project` from `Project.getActiveProject()`. `getRootItem()` returns the root `FolderItem`
([Project class](https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/project/)).
You get the module with `require("premierepro")`
([DOM APIs](https://developer.adobe.com/premiere-pro/uxp/resources/fundamentals/dom-apis/)).
The paths are plain strings, and clipforage already returns absolute local paths
(`JobOut.path`, `HistoryRow.path`).

**(unconfirmed)**:
- Whether `importFiles` needs any `localFileSystem` permission for paths outside the sandbox.
  The [filesystem recipe](https://developer.adobe.com/premiere-pro/uxp/resources/recipes/filesystem-operations/)
  only says that reading arbitrary `file:/` paths through the UXP fs API needs `"fullAccess"`.
- Whether the `FolderItem` from `getRootItem()` can be passed straight in as `targetBin`
  (typed `ProjectItem`).
- Whether `importFiles` has to run inside `project.lockedAccess()`. The 26.3 changelog requires
  that wrapper for *action creation*, and `importFiles` is not documented as an action.

**Option B, drag and drop (27.0.0+)**
([tutorial](https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/drag-and-drop/)):

- Needs manifest v5 or later with a panel entrypoint, plus
  `"requiredPermissions": {"localFileSystem": "request"}`.
- The element must have `draggable="true"`. On `dragstart`, call
  `event.dataTransfer.setData('text/plain', JSON.stringify(payload))` with
  `{version: "1.0.0", items: [{name, display_name?, content_type, uri}]}`. `version` must be
  exactly `"1.0.0"`.
- **`uri` must be `file://`.** "Third-party panels may reference local files only. Always use the
  `file://` scheme." Windows form: `file:///C:/path/clip.mov`, run through `encodeURI()`.
  **An http URL is not supported**, so `/api/library/{id}/file` cannot be the drag source.
- Drop targets: the Project panel (Icon, List or Freeform view, including onto a bin) or an open
  sequence in the Timeline.
- Supported `content_type` values include `video/mp4`, `video/quicktime`, `audio/mpeg`,
  `audio/mp3`, `audio/wav`, `image/png`, `image/jpeg`, `image/gif` and `image/webp`. **`video/webm`
  is not listed**, so webm downloads would be skipped.

## 2. Distribution

- **Local development:** install UDT from the Creative Cloud desktop app; enable Developer Mode
  on first launch (this needs elevation). To enable it by hand, write `{ "developer" : true }` to
  `settings.json` under `%CommonProgramFiles%/Adobe/UXP/Developer` (Windows) or
  `/Library/Application Support/Adobe/UXP/Developer` (macOS)
  ([dev tools](https://developer.adobe.com/premiere-pro/uxp/introduction/essentials/dev-tools/)).
  Use Add Plugin, pick `manifest.json`, then Open
  ([plugin management](https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/udt-deep-dive/plugin-management/)),
  then Load & Watch ([plugins](https://developer.adobe.com/premiere-pro/uxp/plugins/)).
  Changes to `manifest.json` need an explicit unload and reload.
- **Packaging:** in UDT, use the ••• menu > Package to get a `.ccx`. "You **no longer need to
  apply a Digital Signature and Timestamp the package**." Only hybrid plugins with native
  `.uxpaddon` binaries need Apple signing and notarization. A marketplace listing needs an ID from
  the Developer Distribution portal that matches `manifest.json`
  ([package](https://developer.adobe.com/premiere-pro/uxp/plugins/distribution/package/)).
- **Channels:** the Creative Cloud Marketplace needs Adobe review. Direct distribution (your own
  site, GitHub, a third-party store) needs "no review or approval". All channels use `.ccx`
  ([overview](https://developer.adobe.com/premiere-pro/uxp/plugins/distribution/overview/)).
  Users who install an independent `.ccx` see "the warning dialog that suggests to only install
  plugins from trusted sources"
  ([independent distribution](https://developer.adobe.com/premiere-pro/uxp/plugins/distribution/independent-distribution/)).
- **For clipforage:** an unsigned `.ccx` attached to a GitHub release is a documented, supported
  path, so users do not need UDT or Developer Mode. Asking end users to run UDT is acceptable
  only for contributors. **(unconfirmed)** The exact install step for a `.ccx` outside the
  marketplace (double-click into Creative Cloud desktop, or a CLI). The independent-distribution
  page only describes the warning dialog.

## 3. Design sketch: "clipforage" panel

```
[ search box ] [kind v] [Search]
+------------------------------------------+
| thumb | title              | CC0-1.0 [OK]|  <- license badge; red if commercial_ok=false,
|       | provider - 0:12    | [Get]       |     grey if unknown (null)
+------------------------------------------+
| ... downloaded items: draggable rows ... |
```

1. Search: `GET http://127.0.0.1:8765/api/search?q=...&kind=...&limit=...` returns
   `SearchResponse {assets[], errors[], linkouts[]}`. Each asset carries `ref`, `title`,
   `license`, `commercial_ok`, `attribution_text`, `preview_url`, `provider`.
2. Show provider errors inline, the way the web UI does. One failing provider must not blank the
   list.
3. Get: `POST /api/download` with JSON `{ref, mp3}` and header `X-Clipforage: 1` returns 202
   `{job_id}`. Poll `GET /api/jobs/{job_id}` until `status` is `done` or `error`. `JobOut.path`
   is the absolute local file.
4. Import: either (a) an "Import" button calling
   `project.importFiles([path], true, rootItem, false)` (25.6+), or (b) mark the row
   `draggable` and send the `file://` payload (27.0+). Detect support at runtime and fall back to
   (a).
5. Library tab: `GET /api/history` returns rows with `path` and `license`, so earlier downloads
   can be re-imported or dragged.
6. Attribution: write `attribution_text` into the clip's metadata or a marker
   **(not researched; API unconfirmed)**. As a minimum, show it in the panel with a copy button.

What the Python side must add:

- **Serving files: nothing.** Both import routes take local paths, so `/api/library/{id}/file`
  is not needed by the panel.
- **CORS:** maybe. If UXP enforces CORS (the docs hint it does, but this is unconfirmed), add a
  narrowly scoped `CORSMiddleware` allowing the panel's origin, `GET`/`POST`, and the
  `X-Clipforage` header. The origin value UXP sends is unknown and must be measured on a real
  install before anything is allow-listed. Never use `*` (see the ARCHITECTURE.md security
  model).
- **Host check:** a fetch to `http://127.0.0.1:8765` should send `Host: 127.0.0.1:8765`, which
  `TrustedHostMiddleware` already allows (standard fetch behaviour; not verified in UXP).
- **`X-Clipforage` header:** the panel sets it on POSTs. No server change is needed.
- **Format:** prefer an mp4/h264 output for Premiere. webm is not in the drag content-type list,
  and [KNOWN_ISSUES.md](KNOWN_ISSUES.md) already notes AV1 problems.

## 4. Risks

- **Version gating:** the best UX, drag to the timeline, needs Premiere 27.0.0, which may not be
  generally available yet (unconfirmed). Button import needs 25.6+. Users on Premiere releases
  before 25.6 get nothing from a UXP panel.
- **macOS http:** Adobe says macOS restricts `http://`. If that covers loopback, the panel cannot
  reach clipforage on macOS without TLS on localhost, which is a large change to `serve`.
  Unconfirmed, and possibly blocking on macOS.
- **CORS / origin:** unknown behaviour. A loose fix (`allow_origins=["*"]`) would undercut the
  local security model.
- **API churn:** the changelog shows breaking changes in 26.3 (`Sequence.setSelection()` became
  synchronous, `project.lockedAccess()` became mandatory for action creation) and deprecations in
  26.5. A panel needs maintenance roughly once per Premiere release.
- **Maintenance burden:** a second, JS-based UI that duplicates the web UI, in a host the
  maintainer cannot test in CI. It needs Premiere installed (a paid CC subscription) for every
  change.
- **Windows/macOS differences:** `file://` URI formatting differs (drive-letter rewrite on
  Windows, per the tutorial code), and there is the macOS http issue above. The Developer Mode
  settings path differs by OS and needs admin rights.
- **Install friction:** independent `.ccx` installs show a "trusted sources" warning.

## 5. Effort

- Spike-to-working panel (search, download, poll, `importFiles`, license badges), Windows only,
  on one Premiere version: **M**.
- Plus drag and drop, macOS (possibly TLS on loopback), CORS hardening, `.ccx` release packaging
  and per-release upkeep: **L**.

## 6. Recommendation: GO-LATER

Do not build the panel in M4. Revisit once (a) Premiere 27.0 drag support is generally available,
and (b) someone with Premiere has run the skeleton and answered the three blocking unknowns:
loopback `fetch` with an `http://127.0.0.1:8765` domain entry, CORS/Origin behaviour, and macOS
`http://` to loopback.

Reasoning:

- The project's default integration (folder import / OS drag-out from the web UI) already works
  in Premiere and every other editor ([RESEARCH.md](RESEARCH.md), "Editor integration options").
- The Resolve `ImportMedia` script is the M4 deliverable. The panel adds convenience, not a new
  capability.
- The UXP pieces needed do exist and are documented: `fetch`, `importFiles`, a `file://`
  drag payload, and unsigned `.ccx` distribution. So this is not a NO-GO.
- The two highest-value parts (timeline drag, macOS) depend on a 27.0 gate and on unconfirmed
  network behaviour. Each Premiere release also brings breaking-change upkeep that the
  maintainer cannot cover in CI.

Next step when revisited: load `spikes/premiere-uxp/` with UDT on Windows and Premiere 25.6+, then
record the observed loopback/CORS/Origin behaviour in this file.

## Sources fetched for this spike

- https://developer.adobe.com/premiere-pro/uxp/
- https://developer.adobe.com/premiere-pro/uxp/introduction/
- https://developer.adobe.com/premiere-pro/uxp/changelog/
- https://developer.adobe.com/premiere-pro/uxp/plugins/
- https://developer.adobe.com/premiere-pro/uxp/plugins/concepts/manifest/
- https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/drag-and-drop/
- https://developer.adobe.com/premiere-pro/uxp/plugins/tutorials/udt-deep-dive/plugin-management/
- https://developer.adobe.com/premiere-pro/uxp/introduction/essentials/dev-tools/
- https://developer.adobe.com/premiere-pro/uxp/resources/recipes/network/
- https://developer.adobe.com/premiere-pro/uxp/resources/recipes/filesystem-operations/
- https://developer.adobe.com/premiere-pro/uxp/resources/fundamentals/dom-apis/
- https://developer.adobe.com/premiere-pro/uxp/resources/faq/ (no relevant entries)
- https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/project/
- https://developer.adobe.com/premiere-pro/uxp/plugins/distribution/overview/
- https://developer.adobe.com/premiere-pro/uxp/plugins/distribution/package/
- https://developer.adobe.com/premiere-pro/uxp/plugins/distribution/independent-distribution/
- context7 `/websites/developer_adobe_premiere-pro_uxp` snippets: inter-plugin-comm tutorial
  manifest, UDT plugin-workflows (Load & Watch, manifest reload), typescript-support
  (`require("premierepro")`, `Project.getActiveProject()`).
