// UNTESTED SPIKE: never loaded into Premiere. See docs/PREMIERE_UXP_SPIKE.md.
//
// Calls the local clipforage web API and renders result titles + license badges.
// Confirmed in Adobe docs: global fetch() exists in UXP and is gated by
// manifest requiredPermissions.network.domains
// (https://developer.adobe.com/premiere-pro/uxp/resources/recipes/network/).
// NOT confirmed: that http://127.0.0.1:8765 (http, loopback, explicit port) is accepted
// as a domain entry, that macOS allows http to loopback, and whether UXP applies CORS.

const API = "http://127.0.0.1:8765";

const form = document.getElementById("search-form");
const qInput = document.getElementById("q");
const kindSelect = document.getElementById("kind");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");

function setStatus(text) {
  statusEl.textContent = text;
}

function licenseBadge(asset) {
  const badge = document.createElement("span");
  badge.className = "badge";
  if (asset.commercial_ok === true) badge.classList.add("ok");
  if (asset.commercial_ok === false) badge.classList.add("nc");
  badge.textContent = asset.license;
  badge.title = asset.attribution_text || "";
  return badge;
}

function render(data) {
  resultsEl.textContent = "";
  for (const asset of data.assets) {
    const li = document.createElement("li");
    const title = document.createElement("span");
    title.className = "title";
    // textContent, not innerHTML: titles come from third-party providers.
    title.textContent = `${asset.title} (${asset.provider})`;
    li.appendChild(title);
    li.appendChild(licenseBadge(asset));
    resultsEl.appendChild(li);
  }
  const errs = data.errors.map((e) => `${e.provider}: ${e.message}`).join("; ");
  setStatus(`${data.assets.length} results${errs ? ` | errors: ${errs}` : ""}`);
}

async function search(q, kind) {
  const params = new URLSearchParams({ q, kind, limit: "20" });
  // GET needs no X-Clipforage header; POST /api/download would (not implemented here).
  const res = await fetch(`${API}/api/search?${params.toString()}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = qInput.value.trim();
  if (!q) return;
  setStatus("Searching...");
  try {
    render(await search(q, kindSelect.value));
  } catch (err) {
    setStatus(`Search failed (is clipforage serve running?): ${err.message}`);
  }
});

// Next steps, intentionally NOT implemented in this spike:
// - POST /api/download {ref} with header "X-Clipforage: 1", poll GET /api/jobs/{job_id}.
// - Import the finished JobOut.path via the documented
//   require("premierepro").Project.getActiveProject() -> project.importFiles(
//     [path], suppressUI, targetBin, asNumberedStills)  (min 25.6), or
// - drag payload {version: "1.0.0", items: [{name, content_type, uri: "file:///C:/..."}]}
//   on dragstart via dataTransfer.setData("text/plain", ...)  (min Premiere 27.0.0).
// Whether importFiles accepts the FolderItem from getRootItem() as targetBin, and whether it
// must run inside project.lockedAccess(), was NOT confirmed in the docs.
