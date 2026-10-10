/* clipforage local web UI. Vanilla JS, no build step, talks only to our own /api. */
"use strict";

(() => {
  const KINDS = [
    "video", "image", "gif", "sticker", "audio_sfx", "audio_music",
    "font", "lut", "overlay", "template",
  ];
  const KIND_LABELS = {
    video: "Video", image: "Image", gif: "GIF", sticker: "Sticker", audio_sfx: "Sound FX",
    audio_music: "Music", font: "Font", lut: "LUT", overlay: "Overlay", template: "Template",
  };
  const KEY_PAGES = {
    pexels: "https://www.pexels.com/api/",
    pixabay: "https://pixabay.com/api/docs/",
    freesound: "https://freesound.org/apiv2/apply/",
    google_fonts: "https://developers.google.com/fonts/docs/developer_api",
    anthropic: "https://console.anthropic.com/settings/keys",
    openai: "https://platform.openai.com/api-keys",
  };
  const STORE_KEY = "clipforage.search";
  const POLL_MS = 700;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const DRAG_HINT = "Drag into your editor or a folder";
  const CHROMA_KEY = "clipforage.chroma";
  const FILTERS_OPEN_KEY = "clipforage.filtersOpen";
  const DISMISSED_KEY = "clipforage.dismissedNotices";
  const JOB_META_KEY = "clipforage.jobMeta";
  // One-time move of the pre-rename memefinder.* keys: read old if new is missing, then drop old.
  for (const [oldKey, newKey] of [["memefinder.search", STORE_KEY], ["memefinder.chroma", CHROMA_KEY]]) {
    try {
      const old = localStorage.getItem(oldKey);
      if (old !== null && localStorage.getItem(newKey) === null) localStorage.setItem(newKey, old);
      if (old !== null) localStorage.removeItem(oldKey);
    } catch (_) { /* storage unavailable: nothing to migrate */ }
  }
  const CHROMA_MAX_WIDTH = 640;
  const CHROMA_SOFTNESS = 24;
  const CHROMA_TOL_MAX = 120;
  const CHROMA_TOL_DEFAULT = 80;
  const CHROMA_FALLBACK_MS = 33;
  const MIME_BY_EXT = {
    mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm", mov: "video/quicktime", mkv: "video/x-matroska",
    mp3: "audio/mpeg", m4a: "audio/mp4", wav: "audio/wav", ogg: "audio/ogg", oga: "audio/ogg", flac: "audio/flac",
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", svg: "image/svg+xml",
    ttf: "font/ttf", otf: "font/otf", woff: "font/woff", woff2: "font/woff2",
    cube: "application/octet-stream", json: "application/json", zip: "application/zip",
  };
  // Static icon path data (our own constants, never API data).
  const KIND_ICONS = {
    video: "M4 6h11v12H4z M15 10l5-3v10l-5-3z",
    image: "M4 5h16v14H4z M4 16l5-5 4 4 3-3 4 4",
    gif: "M4 5h16v14H4z M4 16l5-5 4 4 3-3 4 4",
    sticker: "M5 5h10l4 4v10H5z M15 5v4h4",
    audio_sfx: "M4 10v4h4l5 4V6L8 10z M16 9a4 4 0 0 1 0 6",
    audio_music: "M9 18V6l10-2v12 M9 18a2 2 0 1 1-4 0 2 2 0 0 1 4 0z M19 16a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
    font: "M5 19L11 5h2l6 14 M8 14h8",
    lut: "M12 4a8 8 0 1 0 0 16z M12 4a8 8 0 0 1 0 16",
    overlay: "M4 8h12v12H4z M8 4h12v12",
    template: "M4 4h16v16H4z M4 10h16 M10 10v10",
  };

  const $ = (id) => document.getElementById(id);

  /* ---------- helpers ---------- */

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [name, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (name === "class") node.className = value;
        else if (name === "text") node.textContent = value;
        else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
        else node.setAttribute(name, value === true ? "" : String(value));
      }
    }
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function kindIcon(kind) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", KIND_ICONS[kind] || KIND_ICONS.image);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.6");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("stroke-linecap", "round");
    svg.append(path);
    return svg;
  }

  function placeholder(kind) {
    return el("span", { class: "placeholder" }, kindIcon(kind), el("span", { text: KIND_LABELS[kind] || kind.replace("_", " ") }));
  }

  function errorMessage(data, status) {
    if (data && typeof data.detail === "string") {
      return data.hint ? `${data.detail} (${data.hint})` : data.detail;
    }
    if (data && Array.isArray(data.detail)) {
      return data.detail.map((d) => (d && d.msg) || String(d)).join("; ");
    }
    return `HTTP ${status}`;
  }

  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (method !== "GET") opts.headers["X-Clipforage"] = "1";
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = null;
    const type = res.headers.get("content-type") || "";
    if (type.includes("application/json")) data = await res.json();
    if (!res.ok) {
      const err = new Error(errorMessage(data, res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function toast(message, isError, title) {
    const node = el("div", { class: isError ? "toast error" : "toast", text: message, title });
    $("toasts").append(node);
    setTimeout(() => node.remove(), isError ? 7000 : 3500);
  }

  function formatDuration(seconds) {
    const s = Math.round(seconds);
    const m = Math.floor(s / 60);
    const h = Math.floor(m / 60);
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m % 60)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
  }

  function formatWhen(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  function licenseBadge(asset) {
    let cls = "badge lic-unknown";
    let text = asset.license || "unknown";
    let title = "License terms unknown; check the source";
    if (asset.commercial_ok === true) {
      cls = "badge lic-ok";
      title = "Commercial use allowed";
    } else if (asset.commercial_ok === false) {
      cls = "badge lic-nc";
      if (!/\bnc\b/i.test(text)) text = `${text} NC`;
      title = "Not for commercial use";
    } else if (!asset.license || asset.license === "unknown") {
      // Unknown terms: say what to do, and link straight to the source when we have one.
      text = "Check license";
      title = "License not stated by the source; open Source to check";
      if (/^https?:\/\//.test(asset.source_url || "")) {
        return el("a", {
          class: `${cls} badge-link`,
          href: asset.source_url,
          target: "_blank",
          rel: "noopener noreferrer",
          title,
          text,
        });
      }
    }
    return el("span", { class: cls, title, text });
  }

  const RIGHTS_LEVELS = ["clear", "attribution", "noncommercial", "copyrighted", "unknown"];

  // Rights-level badge from asset.rights (label as text, detail as tooltip). Unknown links to
  // the source so the user can check; assets without rights fall back to the license badge.
  function renderRightsBadge(asset) {
    const rights = asset.rights;
    if (!rights || !RIGHTS_LEVELS.includes(rights.level)) return licenseBadge(asset);
    const attrs = { class: `badge rights-${rights.level}`, title: rights.detail, text: rights.label };
    if (rights.level === "unknown" && /^https?:\/\//.test(asset.source_url || "")) {
      return el("a", {
        ...attrs,
        class: `${attrs.class} badge-link`,
        href: asset.source_url,
        target: "_blank",
        rel: "noopener noreferrer",
      });
    }
    return el("span", attrs);
  }

  // Copyrighted audio: a small warning glyph on a download button, the detail as its tooltip.
  // Called again on reused buttons (lightbox), so it first drops any earlier glyph.
  function setRightsWarning(btn, asset) {
    const old = btn.querySelector(".rights-warn");
    if (old) old.remove();
    const rights = asset && asset.rights;
    if (!rights || rights.level !== "copyrighted" || !isAudio(asset)) return;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M12 4L21 20H3z M12 10v4.5 M12 17.2v.3");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("stroke-linecap", "round");
    svg.append(path);
    const glyph = el("span", { class: "rights-warn", role: "img", "aria-label": "copyright warning", title: rights.detail });
    glyph.append(svg);
    btn.append(glyph);
  }

  const NO_CREDIT_LICENSE = /\bcc0\b|public domain|\bpdm?\b|pexels|pixabay/i;

  // "credit" chip when the asset carries attribution text and its license is not a no-credit one.
  function creditChip(asset) {
    const text = (asset.attribution_text || "").trim();
    if (!text || NO_CREDIT_LICENSE.test(asset.license || "")) return null;
    return el("span", {
      class: "badge credit",
      title: `Credit required: ${text}`,
      "aria-label": `Credit required: ${text}`,
      text: "credit",
    });
  }

  function previewUrl(asset) {
    return `/api/preview/${encodeURIComponent(asset.provider)}/${encodeURIComponent(asset.id)}`;
  }

  function mediaUrl(asset) {
    return `/api/media/${encodeURIComponent(asset.provider)}/${encodeURIComponent(asset.id)}`;
  }

  const AUDIO_KINDS = ["audio_sfx", "audio_music"];
  const YOUTUBE_ID = /^[\w-]{11}$/;

  function isAudio(asset) {
    return AUDIO_KINDS.includes(asset.kind);
  }

  // A playable preview exists: the provider gave media_url, or IA (resolved server-side).
  function hasMedia(asset) {
    return Boolean(asset.media_url) || asset.provider === "internet_archive";
  }

  function isYouTube(asset) {
    return asset.provider === "youtube" && YOUTUBE_ID.test(asset.id);
  }

  // Only one preview plays at a time across cards and the lightbox.
  document.addEventListener("play", (ev) => {
    for (const node of document.querySelectorAll("audio, video")) {
      if (node !== ev.target) node.pause();
    }
  }, true);

  /* ---------- drag-out (Chromium DownloadURL) ---------- */

  function baseName(path) {
    return String(path || "").split(/[\\/]/).pop() || "download";
  }

  function mimeFor(name) {
    const dot = name.lastIndexOf(".");
    const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
    return MIME_BY_EXT[ext] || "application/octet-stream";
  }

  function gripIcon() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 10 16");
    svg.setAttribute("class", "grip");
    svg.setAttribute("aria-hidden", "true");
    for (const [cx, cy] of [[3, 3], [7, 3], [3, 8], [7, 8], [3, 13], [7, 13]]) {
      const dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("cx", String(cx));
      dot.setAttribute("cy", String(cy));
      dot.setAttribute("r", "1.3");
      svg.append(dot);
    }
    const wrap = el("span", { class: "grip-wrap", title: DRAG_HINT });
    wrap.append(svg);
    return wrap;
  }

  // True between dragstart and dragend; refreshJobs leaves the drawer DOM alone meanwhile.
  let dragging = false;
  // Document-level guards: per-node dragend never reaches us if the node was re-rendered mid-drag.
  const resetDragging = () => { dragging = false; };
  document.addEventListener("dragend", resetDragging);
  document.addEventListener("drop", resetDragging);
  document.addEventListener("mouseup", resetDragging);

  // Make `node` drag out the library file of history row `id`. Without an id it stays inert.
  function makeDraggable(node, id, path) {
    if (id === null || id === undefined) return node;
    const name = baseName(path);
    const url = `${location.origin}/api/library/${encodeURIComponent(id)}/file`;
    node.setAttribute("draggable", "true");
    node.classList.add("draggable");
    node.dataset.dragUrl = url;
    if (!node.getAttribute("title")) node.setAttribute("title", DRAG_HINT);
    node.addEventListener("dragend", () => { dragging = false; });
    node.addEventListener("dragstart", (ev) => {
      dragging = true;
      const dt = ev.dataTransfer;
      if (!dt) return;
      try {
        dt.effectAllowed = "copy";
        dt.setData("DownloadURL", `${mimeFor(name)}:${name}:${url}`);
      } catch (_) { /* non-Chromium: DownloadURL unsupported, the URL types still work */ }
      try {
        dt.setData("text/uri-list", url);
        dt.setData("text/plain", url);
      } catch (_) { /* never let a drag throw */ }
    });
    return node;
  }

  function openPath(path) {
    return api("POST", "/api/open", { path })
      .then((res) => toast(`Opened ${res.opened}`))
      .catch((err) => toast(`Could not open folder: ${err.message}`, true));
  }

  /* ---------- tabs ---------- */

  const tabButtons = Array.from(document.querySelectorAll(".tabs [role=tab]"));

  function selectTab(name, focus) {
    for (const btn of tabButtons) {
      const on = btn.dataset.tab === name;
      btn.setAttribute("aria-selected", String(on));
      btn.tabIndex = on ? 0 : -1;
      $(`panel-${btn.dataset.tab}`).hidden = !on;
      if (on && focus) btn.focus();
    }
    // The drawer would cover the History table / Settings: close it when leaving Search.
    if (name !== "search") toggleDrawer(false);
    if (name === "history") loadHistory();
    if (name === "settings") loadSettings();
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => selectTab(btn.dataset.tab));
    btn.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
      const i = tabButtons.indexOf(btn);
      const next = tabButtons[(i + (ev.key === "ArrowRight" ? 1 : -1) + tabButtons.length) % tabButtons.length];
      selectTab(next.dataset.tab, true);
    });
  }

  /* ---------- doctor ---------- */

  let doctorData = null;

  function setDoctorStatus(ok, title) {
    $("doctor-dot").className = ok ? "status-dot ok" : "status-dot bad";
    $("doctor-label").textContent = ok ? "Tools OK" : "Tools: issues";
    $("doctor-status").title = title;
  }

  async function loadDoctor() {
    try {
      doctorData = await api("GET", "/api/doctor");
    } catch (err) {
      setDoctorStatus(false, `Doctor check failed: ${err.message}`);
      return;
    }
    const missing = [];
    if (!doctorData.ffmpeg.found) missing.push("ffmpeg");
    if (!doctorData.deno.found) missing.push("deno");
    const ok = missing.length === 0;
    setDoctorStatus(ok, ok
      ? `Tools OK: ffmpeg, deno, yt-dlp ${doctorData.yt_dlp.version}`
      : `Missing: ${missing.join(", ")}. Run \`clipforage setup\``);
    renderDoctor();
  }

  function renderDoctor() {
    const list = $("doctor-list");
    if (!doctorData) return;
    const row = (name, ok, detail) =>
      el("li", null,
        el("span", { class: ok ? "status-dot ok" : "status-dot bad", "aria-hidden": "true" }),
        el("span", { text: name }),
        el("span", { class: "detail", text: detail }));
    const d = doctorData;
    list.replaceChildren(
      row("ffmpeg", d.ffmpeg.found, d.ffmpeg.found ? d.ffmpeg.path : "not found"),
      row("deno", d.deno.found, d.deno.found ? d.deno.path : "not found"),
      row("yt-dlp", Boolean(d.yt_dlp.version), d.yt_dlp.version || "not installed"),
      row("yt-dlp-ejs", d.ejs, d.ejs ? "installed" : "not installed"),
    );
    if (!d.ffmpeg.found || !d.deno.found) {
      list.append(el("li", { class: "doctor-hint" }, "Run ", el("code", { text: "clipforage setup" }), " to fetch the missing tools."));
    }
  }

  /* ---------- search form ---------- */

  const form = $("search-form");
  const kindSelect = $("kind");
  const providerSelect = $("provider");
  let providers = [];
  let lockedProviders = new Set(); // need a key and have none; the key-hint covers them

  for (const kind of KINDS) {
    kindSelect.append(el("option", { value: kind, text: KIND_LABELS[kind] }));
  }
  kindSelect.value = "video";

  const STYLES = ["any", "meme", "green_screen", "cinematic"];
  const QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"];

  function readStyle() {
    const checked = form.querySelector('input[name="style"]:checked');
    return checked ? checked.value : "any";
  }

  const MODES = ["assets", "phrase", "describe"];
  const PHRASE_SOURCES = ["youtube", "internet_archive"];
  const ASSETS_PLACEHOLDER = $("q").placeholder;
  const PHRASE_PLACEHOLDER = 'a spoken line, e.g. "never gonna give you up"';
  const DEFAULT_KIND = "video";
  const describeText = $("describe-text");

  function readMode() {
    const checked = form.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "assets";
  }

  function readSources() {
    return [...form.querySelectorAll('input[name="source"]:checked')].map((box) => box.value);
  }

  // Describe has its own Kind, defaulting to "Any kind" (sent as kind: null so assist decides);
  // Assets (and Phrase, which hides Kind) keep theirs. The extra option exists only in Describe.
  const ANY_KIND_OPTION = el("option", { value: "", text: "Any kind (let assist decide)" });
  const kindByGroup = { assets: DEFAULT_KIND, describe: "" };
  let kindGroup = "assets";

  function syncKindToMode(mode) {
    const group = mode === "describe" ? "describe" : "assets";
    if (group === kindGroup) return;
    kindByGroup[kindGroup] = kindSelect.value;
    if (group === "describe") kindSelect.prepend(ANY_KIND_OPTION);
    else ANY_KIND_OPTION.remove();
    kindSelect.value = kindByGroup[group];
    if (kindSelect.value !== kindByGroup[group]) kindSelect.value = group === "describe" ? "" : DEFAULT_KIND;
    kindGroup = group;
  }

  function applyMode() {
    const mode = readMode();
    form.dataset.mode = mode;
    syncKindToMode(mode);
    $("q").placeholder = mode === "phrase" ? PHRASE_PLACEHOLDER : ASSETS_PLACEHOLDER;
    // A hidden required control blocks submit: only the visible query box is required.
    $("q").required = mode !== "describe";
    describeText.required = mode === "describe";
    const card = $("assist-card");
    card.hidden = mode !== "describe" || !card.childElementCount;
    renderSafeStatus(0, false); // the line belongs to the other mode's last search
    if (mode !== "assets") {
      hideRecall();
      $("results-help").hidden = true; // few-results help is Assets-only
    }
  }

  function setMode(mode) {
    $(`mode-${mode}`).checked = true;
    applyMode();
    updateFiltersCount();
    saveForm();
  }

  for (const radio of form.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", () => {
      applyMode();
      saveForm();
    });
  }

  // Ctrl/Cmd+Enter sends the description; plain Enter is a newline.
  describeText.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || !(ev.ctrlKey || ev.metaKey)) return;
    ev.preventDefault();
    form.requestSubmit();
  });

  // Saved on change too: the limit is used by later clip downloads, not by the search itself.
  $("clip-max-wall").addEventListener("change", saveForm);
  $("quality").addEventListener("change", saveForm);

  /* secondary filters: a <details> disclosure, closed by default below 720px, always open above */

  const filtersBox = $("filters");
  const wideScreen = window.matchMedia("(min-width: 720px)");
  const FILTER_DEFAULTS = {
    orientation: "", min_duration: "", max_duration: "", provider: "", commercial_only: false,
    cleared_only: false, shorts: false, quality: "best", within: "", max_videos: "10",
    clip_max_wall_s: "",
  };
  const ASSET_FILTERS = [
    "orientation", "min_duration", "max_duration", "provider", "commercial_only", "cleared_only",
    "shorts",
  ];
  const PHRASE_FILTERS = ["within", "max_videos", "clip_max_wall_s"];

  function readFiltersOpen() {
    try {
      return localStorage.getItem(FILTERS_OPEN_KEY) === "1";
    } catch (_) {
      return false;
    }
  }

  function applyFiltersLayout() {
    filtersBox.open = wideScreen.matches ? true : readFiltersOpen();
  }

  filtersBox.addEventListener("toggle", () => {
    if (wideScreen.matches) return; // desktop forces it open; only narrow screens remember
    try {
      localStorage.setItem(FILTERS_OPEN_KEY, filtersBox.open ? "1" : "0");
    } catch (_) { /* storage unavailable: not fatal */ }
  });
  wideScreen.addEventListener("change", applyFiltersLayout);

  // Names of the non-default secondary filters that apply to the current mode.
  function activeFilterNames(f) {
    const names = (f.mode === "phrase" ? PHRASE_FILTERS : ASSET_FILTERS).concat("quality");
    return names.filter((name) => f[name] !== FILTER_DEFAULTS[name]);
  }

  function updateFiltersCount() {
    const n = activeFilterNames(readForm()).length;
    const badge = $("filters-count");
    badge.textContent = String(n);
    badge.hidden = n === 0;
    filtersBox.querySelector("summary").setAttribute("aria-label", n ? `Filters, ${n} active` : "Filters");
  }

  form.addEventListener("input", updateFiltersCount);
  form.addEventListener("change", updateFiltersCount);

  function readForm() {
    return {
      mode: readMode(),
      sources: readSources(),
      within: $("within").value.trim(),
      max_videos: $("max-videos").value,
      clip_max_wall_s: $("clip-max-wall").value.trim(),
      quality: $("quality").value,
      q: $("q").value.trim(),
      describe: describeText.value.trim(),
      kind: kindSelect.value, // "" only in Describe: any kind
      assets_kind: kindGroup === "assets" ? kindSelect.value : kindByGroup.assets,
      describe_kind: kindGroup === "describe" ? kindSelect.value : kindByGroup.describe,
      style: readStyle(),
      orientation: $("orientation").value,
      commercial_only: $("commercial").checked,
      cleared_only: $("cleared").checked,
      shorts: $("shorts").checked,
      min_duration: $("min-duration").value,
      max_duration: $("max-duration").value,
      provider: providerSelect.value,
    };
  }

  function saveForm() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(readForm()));
    } catch (_) {
      /* storage may be unavailable; not fatal */
    }
  }

  function restoreForm() {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    } catch (_) {
      saved = null;
    }
    if (!saved || typeof saved !== "object") return null;
    // The query is not restored: a stale search from an earlier session is noise.
    // Older saves have only `kind`: it was shared by Assets and Describe, so it seeds Assets.
    const assetsKind = KINDS.includes(saved.assets_kind) ? saved.assets_kind : saved.kind;
    if (KINDS.includes(assetsKind)) kindSelect.value = assetsKind;
    if (saved.describe_kind === "" || KINDS.includes(saved.describe_kind)) kindByGroup.describe = saved.describe_kind;
    if (STYLES.includes(saved.style)) $(`style-${saved.style}`).checked = true;
    if (["", "landscape", "portrait", "square"].includes(saved.orientation)) $("orientation").value = saved.orientation;
    $("commercial").checked = Boolean(saved.commercial_only);
    $("cleared").checked = Boolean(saved.cleared_only);
    $("shorts").checked = Boolean(saved.shorts);
    if (typeof saved.describe === "string") describeText.value = saved.describe;
    if (typeof saved.min_duration === "string") $("min-duration").value = saved.min_duration;
    if (typeof saved.max_duration === "string") $("max-duration").value = saved.max_duration;
    if (MODES.includes(saved.mode)) $(`mode-${saved.mode}`).checked = true;
    if (Array.isArray(saved.sources)) {
      for (const id of PHRASE_SOURCES) $(`source-${id}`).checked = saved.sources.includes(id);
    }
    if (typeof saved.within === "string") $("within").value = saved.within;
    if (typeof saved.max_videos === "string" && saved.max_videos !== "") $("max-videos").value = saved.max_videos;
    if (typeof saved.clip_max_wall_s === "string") $("clip-max-wall").value = saved.clip_max_wall_s;
    if (QUALITIES.includes(saved.quality)) $("quality").value = saved.quality;
    applyMode();
    return saved;
  }

  // "Clear filters": back to defaults, keeping the query and mode.
  function clearFilters() {
    kindSelect.value = DEFAULT_KIND;
    $("cleared").checked = FILTER_DEFAULTS.cleared_only;
    $("style-any").checked = true;
    $("orientation").value = FILTER_DEFAULTS.orientation;
    $("min-duration").value = FILTER_DEFAULTS.min_duration;
    $("max-duration").value = FILTER_DEFAULTS.max_duration;
    providerSelect.value = FILTER_DEFAULTS.provider;
    $("commercial").checked = FILTER_DEFAULTS.commercial_only;
    $("shorts").checked = FILTER_DEFAULTS.shorts;
    $("quality").value = FILTER_DEFAULTS.quality;
    updateFiltersCount();
    saveForm();
  }

  async function loadProviders(preferred) {
    try {
      providers = await api("GET", "/api/providers");
    } catch (err) {
      toast(`Could not load providers: ${err.message}`, true);
      return;
    }
    const current = preferred !== undefined ? preferred : providerSelect.value;
    providerSelect.replaceChildren(el("option", { value: "", text: "all" }));
    const locked = [];
    for (const p of providers) {
      const noKey = p.requires_key && !p.has_key;
      if (noKey) locked.push(p.id);
      providerSelect.append(el("option", {
        value: p.id,
        disabled: noKey,
        title: `kinds: ${p.kinds.join(", ")}`,
        text: noKey ? `${p.id} (no key)` : p.id,
      }));
    }
    const usable = providers.some((p) => p.id === current && !(p.requires_key && !p.has_key));
    providerSelect.value = usable ? current : "";
    lockedProviders = new Set(locked);
    updateFiltersCount();

    const hint = $("key-hint");
    if (locked.length) {
      const link = el("a", { href: "#settings", text: "Add keys in Settings" });
      link.addEventListener("click", (ev) => {
        ev.preventDefault();
        selectTab("settings", true);
      });
      hint.replaceChildren(`Need an API key: ${locked.join(", ")}. `, link);
      hint.hidden = false;
    } else {
      hint.hidden = true;
    }
  }

  let searchSeq = 0;

  // First-load explainer: gone for good once any search (or Describe request) starts.
  function hideEmptyState() {
    $("empty-state").hidden = true;
  }

  for (const chip of document.querySelectorAll("#empty-state .example-chip")) {
    chip.addEventListener("click", () => {
      $("mode-assets").checked = true;
      applyMode();
      kindSelect.value = chip.dataset.exampleKind;
      $("q").value = chip.dataset.exampleQ;
      updateFiltersCount();
      $("q").focus(); // the chip is about to disappear with the empty state
      form.requestSubmit();
    });
  }

  const SKELETON_CARDS = 8;
  const SKELETON_GROUPS = 2;

  function skeletonCard() {
    return el("div", { class: "card skeleton-card" },
      el("span", { class: "thumb" }),
      el("div", { class: "card-body" },
        el("span", { class: "sk-line w-90" }),
        el("span", { class: "sk-line w-60" }),
        el("span", { class: "sk-pills" }, el("span", { class: "sk-pill short" }), el("span", { class: "sk-pill" })),
        el("span", { class: "sk-button" })));
  }

  function skeletonGroup() {
    return el("div", { class: "phrase-group skeleton-group" },
      el("div", { class: "sk-head" },
        el("span", { class: "sk-thumb" }),
        el("span", { class: "sk-lines" }, el("span", { class: "sk-line w-90" }), el("span", { class: "sk-line w-60" }))),
      el("span", { class: "sk-line w-90" }),
      el("span", { class: "sk-line w-60" }));
  }

  // Loading placeholders in the list a search fills, but only when it has nothing to show:
  // stale results stay visible (dimmed) instead. clearSkeletons undoes it on error.
  function showSkeletons(id) {
    const list = $(id);
    const other = $(id === "results" ? "phrase-results" : "results");
    const hasContent = (node) => !node.hidden && node.querySelector(".card:not(.skeleton-card), .phrase-group:not(.skeleton-group)");
    if (hasContent(list) || hasContent(other)) return;
    clearSkeletons();
    const make = id === "results" ? skeletonCard : skeletonGroup;
    const count = id === "results" ? SKELETON_CARDS : SKELETON_GROUPS;
    list.dataset.wasHidden = list.hidden ? "1" : "";
    list.replaceChildren(...Array.from({ length: count }, make));
    list.classList.add("is-skeleton");
    list.setAttribute("aria-hidden", "true");
    list.hidden = false;
  }

  function clearSkeletons() {
    for (const id of ["results", "phrase-results"]) {
      const list = $(id);
      if (!list.classList.contains("is-skeleton")) continue;
      list.classList.remove("is-skeleton");
      list.removeAttribute("aria-hidden");
      list.replaceChildren();
      if (list.dataset.wasHidden === "1") list.hidden = true;
      delete list.dataset.wasHidden;
    }
  }

  // In-flight search: busy button + spinner, and the stale results dimmed and inert.
  function setSearching(on) {
    $("search-btn").disabled = on;
    $("search-spinner").hidden = !on;
    for (const id of ["results", "phrase-results"]) {
      if (on) $(id).setAttribute("aria-busy", "true");
      else $(id).removeAttribute("aria-busy");
    }
  }

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const f = readForm();
    if (f.mode === "describe") {
      if (!f.describe) return;
      saveForm();
      runAssist(f);
      return;
    }
    if (!f.q) return;
    saveForm();
    if (f.mode === "phrase") {
      runPhraseSearch(f);
      return;
    }
    runAssetSearch(f);
  });

  let lastAssetSearch = null; // the search behind the current grid ("Search this kind" re-runs it)
  let safeSetting = true; // [search] safe from GET /api/settings; on unless turned off

  // Assets search for `f` (readForm() shape). opts.expand === false: the query alone, no recall
  // expansions. In Assets mode the did-you-mean chips refresh once the results are in.
  // safeOff: this one search runs with safe=false ("Show anyway"); the setting is untouched.
  async function runAssetSearch(f, opts, safeOff) {
    clearTimeout(recallTimer);
    const params = new URLSearchParams({ q: f.q, kind: f.kind });
    if (f.style !== "any") params.set("style", f.style);
    if (f.orientation) params.set("orientation", f.orientation);
    if (f.commercial_only) params.set("commercial_only", "true");
    if (f.cleared_only) params.set("cleared_only", "true");
    if (f.shorts) params.set("shorts", "true");
    if (f.min_duration !== "") params.set("min_duration", f.min_duration);
    if (f.max_duration !== "") params.set("max_duration", f.max_duration);
    if (f.provider) params.set("provider", f.provider);
    if (opts && opts.expand === false) params.set("expand", "false");
    if (safeOff) params.set("safe", "false");

    const seq = ++searchSeq;
    lastAssetSearch = { f, opts, safeOff: Boolean(safeOff) };
    setSearching(true);
    hideEmptyState();
    showSkeletons("results");
    $("result-status").textContent = "Searching…";
    try {
      const data = await api("GET", `/api/search?${params}`);
      if (seq !== searchSeq) return;
      renderResults(data);
      if (readMode() === "assets") loadRecall(f.q, f.kind, data.expanded_queries || []);
    } catch (err) {
      if (seq !== searchSeq) return;
      clearSkeletons();
      $("result-status").textContent = "";
      renderSafeStatus(0, false);
      toast(`Search failed: ${err.message}`, true);
    } finally {
      if (seq === searchSeq) {
        setSearching(false);
      }
    }
  }

  // A search form shape with only query, kind and style set (Describe mode hides the rest).
  function plainSearch(q, kind) {
    return {
      ...readForm(), q, kind, orientation: "", commercial_only: false, cleared_only: false,
      shorts: false, min_duration: "", max_duration: "", provider: "",
    };
  }

  /* ---------- did-you-mean (offline recall) ---------- */

  const RECALL_DEBOUNCE_MS = 300;
  const RECALL_MIN_CHARS = 3;
  const RECALL_MAX = 3;
  let recallTimer = 0;
  let recallSeq = 0;

  function hideRecall() {
    clearTimeout(recallTimer);
    recallSeq += 1;
    $("recall-chips").replaceChildren();
    $("recall-chips").hidden = true;
  }

  // Chip row "Label: a · b" of buttons; empty list hides the row.
  function renderSuggestRow(node, label, items, onPick) {
    if (!items.length) {
      node.replaceChildren();
      node.hidden = true;
      return;
    }
    const parts = [el("span", { class: "label", text: label })];
    items.forEach((text, i) => {
      if (i) parts.push(el("span", { class: "sep", "aria-hidden": "true", text: "·" }));
      parts.push(el("button", { type: "button", class: "suggest-chip", text, onclick: () => onPick(text) }));
    });
    node.replaceChildren(...parts);
    node.hidden = false;
  }

  // Suggestions for `q`, minus the query itself and anything in `exclude` (already searched).
  async function loadRecall(q, kind, exclude) {
    const seq = ++recallSeq;
    if (q.length < RECALL_MIN_CHARS) {
      hideRecall();
      return;
    }
    let data;
    try {
      data = await api("GET", `/api/recall?${new URLSearchParams({ q, kind })}`);
    } catch (_) {
      return; // offline helper: a failure just means no chips
    }
    if (seq !== recallSeq || readMode() !== "assets") return;
    const skip = new Set([q, ...exclude].map((s) => s.toLowerCase()));
    const items = (data.suggestions || []).filter((s) => !skip.has(s.toLowerCase())).slice(0, RECALL_MAX);
    renderSuggestRow($("recall-chips"), "Did you mean:", items, (text) => {
      $("q").value = text;
      form.requestSubmit();
    });
  }

  $("q").addEventListener("input", () => {
    clearTimeout(recallTimer);
    if (readMode() !== "assets") return;
    const q = $("q").value.trim();
    if (q.length < RECALL_MIN_CHARS) {
      hideRecall();
      return;
    }
    recallTimer = setTimeout(() => loadRecall(q, kindSelect.value, []), RECALL_DEBOUNCE_MS);
  });

  /* ---------- describe (assist) ---------- */

  const ASSIST_BACKENDS = {
    claude_cli: "Claude Code",
    anthropic: "Anthropic API",
    openai: "OpenAI",
  };
  let assistStatus = null; // last GET /api/assist/status, for the "Asking <backend>…" label
  let assistSeq = 0;
  let lastAssist = null; // { body, kindDefault } for Retry and the query chips

  function backendName(id) {
    return ASSIST_BACKENDS[id] || id || "the assistant";
  }

  async function loadAssistStatus() {
    try {
      assistStatus = await api("GET", "/api/assist/status");
    } catch (_) {
      assistStatus = null;
    }
    return assistStatus;
  }

  // Which backend a request without an explicit one will use, when the status says.
  function expectedBackend() {
    const s = assistStatus;
    if (!s) return null;
    if (s.configured && s.configured !== "auto") return s.configured;
    return s.auto_pick || null;
  }

  function formatCost(usd) {
    if (typeof usd !== "number" || !Number.isFinite(usd)) return null;
    if (usd > 0 && usd < 0.001) return "<$0.001";
    return `$${usd.toFixed(3)}`;
  }

  function showAssistCard(...children) {
    const card = $("assist-card");
    card.replaceChildren(...children);
    card.hidden = readMode() !== "describe";
  }

  // A link to Settings that scrolls to one block (Assist unless `block` names another).
  function settingsLink(text, block, cls) {
    return el("a", {
      href: "#settings",
      class: cls,
      text: text || "Open Settings",
      onclick: (ev) => {
        ev.preventDefault();
        selectTab("settings", true);
        $(block || "assist-settings").scrollIntoView({ block: "start" });
      },
    });
  }

  function clearResults() {
    clearSkeletons();
    $("results").replaceChildren();
    $("result-status").textContent = "";
    renderSafeStatus(0, false);
    renderAlsoSearched([]);
    renderRelated([], null);
    renderLinkouts([]);
    $("results-help").hidden = true;
  }

  function runAssist(f) {
    // "Any kind" (empty) lets the backend pick; any explicit choice, Video included, is sent.
    const kindDefault = !KINDS.includes(f.kind);
    const body = {
      text: f.describe,
      kind: kindDefault ? null : f.kind,
      style: f.style !== "any" ? f.style : null,
      backend: null,
    };
    lastAssist = { body, kindDefault };
    sendAssist();
  }

  async function sendAssist() {
    if (!lastAssist) return;
    const seq = ++assistSeq;
    searchSeq += 1; // a search still in flight must not land under the new card
    clearSkeletons(); // ...nor leave its loading placeholders behind
    hideEmptyState();
    setSearching(true);
    // Re-check a missing or "off" status (cheap GET): it may have been enabled since.
    if (!assistStatus || assistStatus.enabled === false) await loadAssistStatus();
    if (seq !== assistSeq) return;
    // Assist is opt-in: while it is off, never call /api/assist.
    if (assistStatus && assistStatus.enabled === false) {
      setSearching(false);
      renderAssistOff();
      return;
    }
    const who = expectedBackend();
    showAssistCard(el("div", { class: "assist-loading" },
      el("span", { class: "spinner", "aria-hidden": "true" }),
      el("span", { text: `Asking ${who ? backendName(who) : "the assistant"}…` })));
    let data;
    try {
      data = await api("POST", "/api/assist", lastAssist.body);
    } catch (err) {
      if (seq !== assistSeq) return;
      setSearching(false);
      renderAssistError(err);
      return;
    }
    if (seq !== assistSeq) return;
    setSearching(false);
    renderAssist(data);
  }

  function renderAssistOff() {
    showAssistCard(el("div", { class: "assist-setup" },
      el("h3", { text: "AI assist is off" }),
      el("p", { class: "assist-detail", text: "Describe mode uses an optional AI backend, off by default. Assets and Phrase search, did-you-mean and related results work without it." }),
      el("p", null, settingsLink("Enable in Settings"))));
  }

  function renderAssistError(err) {
    if (err.status === 403) {
      if (assistStatus) assistStatus.enabled = false;
      renderAssistOff();
      return;
    }
    if (err.status === 503) {
      showAssistCard(el("div", { class: "assist-setup" },
        el("h3", { text: "Assist isn't set up" }),
        el("p", { class: "assist-detail", text: err.message }),
        el("p", null, settingsLink(), " to pick a backend or add a key. Assets and Phrase search work without it.")));
      return;
    }
    const title = err.status === 502 ? "Assist failed" : "Could not ask the assistant";
    showAssistCard(el("div", { class: "assist-error" },
      el("h3", { text: title }),
      el("p", { class: "assist-detail", text: err.message }),
      el("button", { type: "button", class: "small", text: "Retry", onclick: () => sendAssist() })));
  }

  // The kind a query chip searches: the backend's hint if the user left Kind on "Any kind".
  function assistKind(data) {
    if (!lastAssist) return KINDS.includes(kindSelect.value) ? kindSelect.value : DEFAULT_KIND;
    if (lastAssist.kindDefault) return KINDS.includes(data.kind_hint) ? data.kind_hint : DEFAULT_KIND;
    return lastAssist.body.kind;
  }

  function renderAssist(data) {
    const queries = Array.isArray(data.queries) ? data.queries.filter(Boolean) : [];
    const names = Array.isArray(data.names) ? data.names.filter(Boolean) : [];
    const kind = assistKind(data);
    const meta = [backendName(data.backend)];
    if (data.model) meta.push(data.model);
    const cost = formatCost(data.cost_usd);
    const head = el("div", { class: "assist-head" },
      el("span", { class: "assist-meta", text: meta.join(" · ") }),
      cost ? el("span", { class: "assist-cost muted", title: "Cost of this request", text: cost }) : null);
    const parts = [head];
    if (names.length) {
      parts.push(el("div", { class: "assist-names", "aria-label": "Possible names" },
        ...names.map((name) => el("span", { class: "name-chip", text: name }))));
    }
    if (data.notes) parts.push(el("p", { class: "assist-notes", text: data.notes }));
    if (data.rights_note) parts.push(el("p", { class: "assist-rights", text: data.rights_note }));
    const chips = queries.map((query) => el("button", {
      type: "button",
      class: "suggest-chip",
      "aria-pressed": "false",
      text: query,
      onclick: (ev) => runAssistQuery(query, kind, ev.currentTarget),
    }));
    if (chips.length) {
      parts.push(el("div", { class: "assist-queries" },
        el("span", { class: "label", text: `Search ${KIND_LABELS[kind] || kind}:` }),
        ...chips));
    } else {
      parts.push(el("p", { class: "muted", text: "No search queries came back. Try describing it differently." }));
    }
    showAssistCard(...parts);
    if (chips.length) runAssistQuery(queries[0], kind, chips[0]);
    else clearResults();
  }

  function runAssistQuery(query, kind, chip) {
    for (const other of $("assist-card").querySelectorAll(".assist-queries .suggest-chip")) {
      other.setAttribute("aria-pressed", String(other === chip));
    }
    runAssetSearch(plainSearch(query, kind));
  }

  let lastPhraseSearch = null; // the phrase search behind the current hits ("Show anyway" re-runs it)

  // safeOff: as in runAssetSearch, this one search runs with safe=false.
  async function runPhraseSearch(f, safeOff) {
    if (!f.sources.length) {
      toast("Pick at least one source for phrase search", true);
      return;
    }
    const params = new URLSearchParams({ q: f.q });
    for (const source of f.sources) params.append("source", source);
    if (f.within) params.set("within", f.within);
    if (f.max_videos !== "") params.set("max_videos", f.max_videos);
    if (safeOff) params.set("safe", "false");

    const seq = ++searchSeq;
    lastPhraseSearch = { f, safeOff: Boolean(safeOff) };
    setSearching(true);
    hideEmptyState();
    showSkeletons("phrase-results");
    $("result-status").textContent = "Searching captions…";
    try {
      const data = await api("GET", `/api/phrase?${params}`);
      if (seq !== searchSeq) return;
      renderPhrase(data, f);
    } catch (err) {
      if (seq !== searchSeq) return;
      clearSkeletons();
      $("result-status").textContent = "";
      renderSafeStatus(0, false);
      toast(`Phrase search failed: ${err.message}`, true);
    } finally {
      if (seq === searchSeq) {
        setSearching(false);
      }
    }
  }

  /* ---------- results ---------- */

  const MISSING_KEY = /\b(?:missing|no)\s+api\s+key\b/i;

  function readDismissed() {
    try {
      const list = JSON.parse(sessionStorage.getItem(DISMISSED_KEY) || "[]");
      return new Set(Array.isArray(list) ? list : []);
    } catch (_) {
      return new Set();
    }
  }

  function rememberDismissed(text) {
    const set = readDismissed();
    set.add(text);
    try {
      sessionStorage.setItem(DISMISSED_KEY, JSON.stringify([...set]));
    } catch (_) { /* storage unavailable: dismissal lasts until the next search */ }
  }

  function renderNotices(errors) {
    const notices = $("notices");
    const dismissed = readDismissed();
    const shown = errors.filter((e) => {
      // The key-hint above the results already says this provider needs a key.
      if (lockedProviders.has(e.provider) && MISSING_KEY.test(e.message)) return false;
      return !dismissed.has(`${e.provider}: ${e.message}`);
    });
    notices.replaceChildren(...shown.map((e) => {
      const text = `${e.provider}: ${e.message}`;
      const li = el("li", null,
        el("span", { class: "notice-text" }, el("span", { class: "notice-provider", text: `${e.provider}: ` }), e.message));
      li.append(el("button", {
        type: "button",
        class: "ghost small",
        "aria-label": `Dismiss ${e.provider} error`,
        text: "Dismiss",
        onclick: () => {
          rememberDismissed(text);
          li.remove();
        },
      }));
      return li;
    }));
  }

  // Under 3 results: active filters as chips, a Clear filters button and tips (assets mode only).
  function renderResultsHelp(count) {
    const help = $("results-help");
    const f = readForm();
    if (count >= 3 || f.mode !== "assets") {
      help.replaceChildren();
      help.hidden = true;
      return;
    }
    const chips = [`Kind: ${KIND_LABELS[f.kind] || f.kind}`];
    if (f.style !== "any") chips.push(`Style: ${$(`style-${f.style}`).nextElementSibling.textContent}`);
    if (f.orientation) chips.push(`Orientation: ${f.orientation}`);
    if (f.min_duration !== "" || f.max_duration !== "") {
      chips.push(`Duration: ${f.min_duration || "0"}–${f.max_duration || "∞"} s`);
    }
    if (f.provider) chips.push(`Provider: ${f.provider}`);
    if (f.commercial_only) chips.push("Commercial use only");
    if (f.shorts) chips.push("Shorts");
    const tips = [];
    if (f.style !== "any") tips.push("Try Style: Any");
    tips.push("Try a different Kind", "Describe it in other words");
    const filtered = f.kind !== "video" || f.style !== "any" || activeFilterNames(f).some((n) => n !== "quality");
    const clear = el("button", {
      type: "button",
      class: "small",
      text: "Clear filters",
      disabled: !filtered,
      onclick: () => {
        clearFilters();
        if ($("q").value.trim()) form.requestSubmit();
      },
    });
    const describe = el("button", {
      type: "button",
      class: "small",
      text: "Describe it instead",
      title: "Switch to Describe mode with this text",
      onclick: () => {
        describeText.value = $("q").value.trim();
        setMode("describe");
        describeText.focus();
      },
    });
    // Safe search is a setting, not a filter: its chip links to Settings (not cleared here).
    const last = f.mode === "phrase" ? lastPhraseSearch : lastAssetSearch;
    const safeOn = safeSetting && !(last && last.safeOff);
    help.replaceChildren(
      el("div", { class: "help-filters" },
        el("span", { class: "label", text: count ? "Few results with:" : "No results with:" }),
        ...chips.map((text) => el("span", { class: "filter-chip", text })),
        safeOn ? settingsLink("Safe search: on", "search-settings", "filter-chip filter-chip-link") : null,
        clear,
        describe),
      el("ul", { class: "help-tips" }, ...tips.map((text) => el("li", { text }))));
    help.hidden = false;
  }

  function renderLinkouts(linkouts) {
    const links = $("linkouts");
    if (linkouts.length) {
      links.replaceChildren(
        el("span", { class: "label", text: "Also try:" }),
        ...linkouts.map((l) => el("a", {
          href: l.url,
          target: "_blank",
          rel: "noopener noreferrer",
          title: l.requires_account ? `${l.license_note} (account required)` : l.license_note,
          text: `${l.name} ↗`,
        })),
      );
      links.hidden = false;
    } else {
      links.replaceChildren();
      links.hidden = true;
    }
  }

  function renderResults(data) {
    clearSkeletons();
    renderNotices(data.errors);
    const n = data.assets.length;
    $("result-status").textContent = n ? `${n} result${n === 1 ? "" : "s"}` : "No results.";
    if (data.relaxed_query) $("result-status").textContent += ` · no cleared results for the full query; showing "${data.relaxed_query}"`;
    renderSafeStatus(data.filtered_count || 0, Boolean(lastAssetSearch && lastAssetSearch.safeOff));
    $("phrase-results").replaceChildren();
    $("phrase-results").hidden = true;
    $("results").hidden = false;
    $("results").replaceChildren(...data.assets.map(renderCard));
    renderAlsoSearched(data.expanded_queries || []);
    renderRelated(data.related || [], data.related_kind || null);
    renderResultsHelp(n);
    renderLinkouts(data.linkouts);
  }

  // Under the result status: "N results hidden by safe search · Show anyway" re-runs the same
  // search once with safe=false; then "Safe search off for this search · Hide again" undoes it.
  function renderSafeStatus(filtered, safeOff) {
    const line = $("safe-status");
    const rerun = (off) => {
      if (readMode() === "phrase") {
        if (lastPhraseSearch) runPhraseSearch(lastPhraseSearch.f, off);
        return;
      }
      if (!lastAssetSearch) return;
      runAssetSearch(lastAssetSearch.f, lastAssetSearch.opts, off);
    };
    if (safeOff) {
      line.replaceChildren("Safe search off for this search",
        el("span", { class: "sep", "aria-hidden": "true", text: " · " }),
        el("button", { type: "button", class: "link-button", text: "Hide again", onclick: () => rerun(false) }));
      line.hidden = false;
    } else if (filtered > 0) {
      line.replaceChildren(`${filtered} result${filtered === 1 ? "" : "s"} hidden by safe search`,
        el("span", { class: "sep", "aria-hidden": "true", text: " · " }),
        el("button", { type: "button", class: "link-button", text: "Show anyway", title: "Run this search once with safe search off (the setting stays on)", onclick: () => rerun(true) }));
      line.hidden = false;
    } else {
      line.replaceChildren();
      line.hidden = true;
    }
  }

  // "Also searched: a · b": the recall expansions the server merged in; a click searches one alone.
  function renderAlsoSearched(queries) {
    renderSuggestRow($("also-searched"), "Also searched:", queries, (text) => {
      $("q").value = text;
      saveForm();
      const f = readMode() === "assets" ? readForm() : plainSearch(text, lastAssetSearch ? lastAssetSearch.f.kind : kindSelect.value);
      runAssetSearch(f, { expand: false });
    });
  }

  let relatedKind = null;

  function renderRelated(assets, kind) {
    relatedKind = assets.length && KINDS.includes(kind) ? kind : null;
    $("related-grid").replaceChildren(...(relatedKind ? assets.map(renderCard) : []));
    $("related-title").textContent = relatedKind ? `Related ${KIND_LABELS[relatedKind]}` : "";
    $("related-search").setAttribute("aria-label", relatedKind ? `Search ${KIND_LABELS[relatedKind]} instead` : "Search this kind");
    $("related").hidden = !relatedKind;
  }

  // Switch Kind to the related one and re-run the search behind the current grid.
  $("related-search").addEventListener("click", () => {
    if (!relatedKind || !lastAssetSearch) return;
    kindSelect.value = relatedKind;
    updateFiltersCount();
    saveForm();
    const { f, opts } = lastAssetSearch;
    runAssetSearch({ ...f, kind: relatedKind, cleared_only: $("cleared").checked && f.mode === "assets" }, opts);
  });

  /* ---------- phrase hits ---------- */

  function formatRange(start, end) {
    return `${formatDuration(Math.floor(start))}–${formatDuration(Math.ceil(end))}`;
  }

  // The cue text with every query word wrapped in <mark> (case-insensitive, whole words).
  function highlight(text, query) {
    const words = (query.toLowerCase().replace(/’/g, "'").match(/[\p{L}\p{N}']+/gu) || [])
      .map((w) => w.replace(/^'+|'+$/g, ""))
      .filter(Boolean);
    if (!words.length) return [text];
    const alts = [...new Set(words)].map((w) => w.replace(/'/g, "['’]"));
    const re = new RegExp(`(?<![\\p{L}\\p{N}])(?:${alts.join("|")})(?![\\p{L}\\p{N}])`, "giu");
    const out = [];
    let last = 0;
    for (const m of text.matchAll(re)) {
      if (m.index > last) out.push(text.slice(last, m.index));
      out.push(el("mark", { text: m[0] }));
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function renderPhrase(data, f) {
    clearSkeletons();
    renderNotices(data.errors);
    renderSafeStatus(data.filtered_count || 0, Boolean(lastPhraseSearch && lastPhraseSearch.safeOff));
    const groups = new Map(); // ref -> hits, in first-seen order
    for (const hit of data.hits) {
      if (!groups.has(hit.ref)) groups.set(hit.ref, []);
      groups.get(hit.ref).push(hit);
    }
    const n = data.hits.length;
    if (n) {
      const v = groups.size;
      $("result-status").textContent = `${n} hit${n === 1 ? "" : "s"} in ${v} video${v === 1 ? "" : "s"}`;
    } else {
      $("result-status").textContent = f.sources.includes("internet_archive")
        ? "No hits. Internet Archive quotes match within indexed films only; name the film in Within to index it."
        : "No hits.";
    }
    $("results").replaceChildren();
    $("results").hidden = true;
    renderAlsoSearched([]);
    renderRelated([], null);
    const list = $("phrase-results");
    list.replaceChildren(...[...groups.values()].map((hits) => renderPhraseGroup(hits, f.q)));
    list.hidden = n === 0;
    renderResultsHelp(n);
    renderLinkouts(data.linkouts);
  }

  function renderPhraseGroup(hits, query) {
    const asset = hits[0].asset;
    const thumb = el("button", {
      type: "button",
      class: "phrase-thumb",
      "aria-label": `Preview ${asset.title}`,
      onclick: () => openLightbox(asset),
    });
    if (asset.preview_url) {
      const img = el("img", { src: previewUrl(asset), alt: "", loading: "lazy", decoding: "async" });
      img.addEventListener("error", () => img.replaceWith(placeholder(asset.kind)));
      thumb.append(img);
    } else {
      thumb.append(placeholder(asset.kind));
    }
    const badges = el("div", { class: "badges" },
      el("span", { class: "badge provider", text: asset.provider }),
      renderRightsBadge(asset),
      creditChip(asset));
    if (hits.some((h) => h.auto_generated)) {
      badges.append(el("span", {
        class: "chip auto",
        title: "Automatic captions may contain recognition errors",
        text: "auto captions",
      }));
    }
    const head = el("div", { class: "phrase-head" },
      thumb,
      el("div", { class: "phrase-meta" },
        el("h3", { class: "card-title", title: asset.title, text: asset.title }),
        badges));
    const rows = el("ol", { class: "phrase-hits" }, ...hits.map((hit) => renderHitRow(hit, query)));
    return el("article", { class: "phrase-group", "data-ref": asset.ref }, head, rows);
  }

  function renderHitRow(hit, query) {
    const range = formatRange(hit.start_s, hit.end_s);
    const state = el("div", { class: "card-state", hidden: true });
    const actions = el("div", { class: "card-actions" });
    const row = el("li", { class: "phrase-hit" },
      el("span", { class: "phrase-time", text: range }),
      el("span", { class: "phrase-text" }, ...highlight(hit.text, query)),
      actions,
      state);
    actions.append(
      el("button", {
        type: "button",
        text: "Preview",
        onclick: () => openLightbox(hit.asset, { start: hit.start_s, caption: `${range}  ${hit.text}`, card: row, clip: hit.clip }),
      }),
      el("button", {
        type: "button",
        class: "primary",
        title: `Download ${hit.clip} s`,
        text: "Download clip",
        onclick: () => startDownload(hit.asset, false, row, hit.clip),
      }));
    return row;
  }

  function renderCard(asset) {
    const thumb = el("button", {
      type: "button",
      class: "thumb",
      "aria-label": `Preview ${asset.title}`,
      onclick: () => openLightbox(asset, { card: thumb.closest(".card") }),
    });
    if (asset.preview_url) {
      const img = el("img", { src: previewUrl(asset), alt: "", loading: "lazy", decoding: "async" });
      img.addEventListener("error", () => img.replaceWith(placeholder(asset.kind)));
      thumb.append(img);
    } else {
      thumb.append(placeholder(asset.kind));
    }
    const chips = el("span", { class: "chips" });
    if (typeof asset.duration_s === "number") chips.append(el("span", { class: "chip", text: formatDuration(asset.duration_s) }));
    if (asset.orientation) chips.append(el("span", { class: "chip", text: asset.orientation }));
    if (chips.childElementCount) thumb.append(chips);
    if (asset.kind === "video" && (hasMedia(asset) || isYouTube(asset))) {
      const play = el("span", { class: "play", "aria-hidden": "true" });
      const svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      const tri = document.createElementNS(SVG_NS, "path");
      tri.setAttribute("d", "M9 7l9 5-9 5z");
      tri.setAttribute("fill", "currentColor");
      svg.append(tri);
      play.append(svg);
      thumb.append(play);
    }

    const state = el("div", { class: "card-state", hidden: true });
    const actions = el("div", { class: "card-actions" });
    const card = el("article", { class: "card", "data-ref": asset.ref },
      thumb,
      el("div", { class: "card-body" },
        el("h3", { class: "card-title", title: asset.title, text: asset.title }),
        el("div", { class: "badges" },
          el("span", { class: "badge provider", text: asset.provider }),
          renderRightsBadge(asset),
          creditChip(asset)),
        isAudio(asset) && hasMedia(asset)
          ? el("audio", { controls: true, preload: "none", src: mediaUrl(asset), "aria-label": `Play ${asset.title}` })
          : null,
        state,
        actions));

    const dl = el("button", { type: "button", class: "primary", text: "Download" });
    dl.addEventListener("click", () => startDownload(asset, false, card));
    setRightsWarning(dl, asset);
    actions.append(dl);
    if (asset.kind === "video" && asset.provider === "youtube") {
      const mp3 = el("button", { type: "button", text: "MP3" });
      mp3.addEventListener("click", () => startDownload(asset, true, card));
      setRightsWarning(mp3, asset);
      actions.append(mp3);
    }
    if (/^https?:\/\//.test(asset.source_url)) {
      actions.append(el("a", {
        class: "button ghost",
        href: asset.source_url,
        target: "_blank",
        rel: "noopener noreferrer",
        text: "Source",
      }));
    }
    if (asset.history_id != null) {
      // Downloaded before (file still in the library): same "Saved" drag handle as after a
      // fresh download, so the card drags out after a re-search or reload.
      const view = { job: { status: "done", history_id: asset.history_id, path: asset.history_path } };
      cardViews.set(card, view);
      paintState(card, view);
    }
    return card;
  }

  /* ---------- lightbox ---------- */

  const lightbox = $("lightbox");
  let lightboxAsset = null;
  // The card (or phrase row) whose download state the lightbox mirrors; a detached stand-in
  // when the lightbox was opened without one. lightboxClip: clip range for phrase previews.
  let lightboxCard = null;
  let lightboxClip = null;

  /* chroma-key preview: draw the <video> into a canvas and key out green in JS */

  const chromaOn = $("chroma-on");
  const chromaTolerance = $("chroma-tolerance");
  const chromaDespill = $("chroma-despill");
  let chroma = null;
  let chromaBg = "checker";

  function readChromaPrefs() {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(CHROMA_KEY) || "null");
    } catch (_) {
      saved = null;
    }
    // v2 flipped the slider (higher keys more); older saved tolerances are reset.
    // v3 added despill; a v2 save keeps on/tolerance and defaults despill on.
    const version = saved ? saved.v : null;
    const valid = (version === 2 || version === 3) && Number.isFinite(saved.tolerance);
    const tol = valid ? Math.min(CHROMA_TOL_MAX, Math.max(0, saved.tolerance)) : CHROMA_TOL_DEFAULT;
    const despill = version === 3 && typeof saved.despill === "boolean" ? saved.despill : true;
    return { on: Boolean(saved && saved.on), tolerance: tol, despill };
  }

  function saveChromaPrefs() {
    try {
      localStorage.setItem(CHROMA_KEY, JSON.stringify({
        v: 3,
        on: chromaOn.checked,
        tolerance: Number(chromaTolerance.value),
        despill: chromaDespill.checked,
      }));
    } catch (_) {
      /* storage may be unavailable; not fatal */
    }
  }

  function chromaMessage(text) {
    const msg = $("chroma-msg");
    msg.textContent = text || "";
    msg.hidden = !text;
  }

  // Green dominant: g beats both r and b by more than threshold (CHROMA_TOL_MAX - tolerance,
  // so a higher slider keys more). Pixels within CHROMA_SOFTNESS below it fade out linearly.
  // Despill (when enabled): any still-visible pixel that is green-dominant gets its green clamped
  // to max(r, b), removing the green fringe on soft edges and spill on the subject.
  function keyPixels(data, threshold) {
    const despill = chromaDespill.checked;
    for (let i = 0; i < data.length; i += 4) {
      const g = data[i + 1];
      const rb = Math.max(data[i], data[i + 2]);
      const diff = g - rb;
      if (diff > threshold) data[i + 3] = 0;
      else if (diff > threshold - CHROMA_SOFTNESS) {
        data[i + 3] = Math.round(255 * (threshold - diff) / CHROMA_SOFTNESS);
      }
      if (despill && diff > 0 && data[i + 3] > 0) data[i + 1] = rb;
    }
  }

  // Key the current video frame into the canvas.
  function renderChroma() {
    if (!chroma) return;
    const { video, canvas, ctx } = chroma;
    if (!video.videoWidth || video.readyState < 2) return;
    const w = Math.min(CHROMA_MAX_WIDTH, video.videoWidth);
    const h = Math.max(1, Math.round(video.videoHeight * (w / video.videoWidth)));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    ctx.drawImage(video, 0, 0, w, h);
    let frame;
    try {
      frame = ctx.getImageData(0, 0, w, h);
    } catch (_) {
      stopChroma();
      chromaOn.checked = false;
      chromaMessage("Chroma preview unavailable: the video is cross-origin (canvas tainted).");
      return;
    }
    keyPixels(frame.data, CHROMA_TOL_MAX - Number(chromaTolerance.value));
    ctx.putImageData(frame, 0, 0);
  }

  // One extra key on the next animation frame (initial frame, seek or slider while paused).
  function redrawChroma() {
    if (!chroma || chroma.redraw) return;
    chroma.redraw = requestAnimationFrame(() => {
      if (!chroma) return;
      chroma.redraw = 0;
      renderChroma();
    });
  }

  // Preferred: once per decoded video frame. Only fires while frames are presented.
  function chromaVideoFrame() {
    if (!chroma) return;
    chroma.vfc = chroma.video.requestVideoFrameCallback(chromaVideoFrame);
    renderChroma();
  }

  // Fallback: requestAnimationFrame capped at ~30 fps, skipped while paused.
  function chromaAnimationFrame(now) {
    if (!chroma) return;
    chroma.raf = requestAnimationFrame(chromaAnimationFrame);
    if (chroma.video.paused || now - chroma.lastKey < CHROMA_FALLBACK_MS) return;
    chroma.lastKey = now;
    renderChroma();
  }

  function startChroma(video) {
    stopChroma();
    chromaMessage("");
    const canvas = el("canvas", {
      class: `chroma-canvas bg-${chromaBg}`,
      title: "Click to play or pause",
      onclick: () => (video.paused ? video.play() : video.pause()),
    });
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    video.classList.add("chroma-source");
    video.after(canvas);
    video.addEventListener("seeked", redrawChroma);
    video.addEventListener("loadeddata", redrawChroma);
    chroma = { video, canvas, ctx, raf: 0, vfc: 0, redraw: 0, lastKey: -Infinity };
    if (typeof video.requestVideoFrameCallback === "function") {
      chroma.vfc = video.requestVideoFrameCallback(chromaVideoFrame);
    } else {
      chroma.raf = requestAnimationFrame(chromaAnimationFrame);
    }
    redrawChroma();
  }

  function stopChroma() {
    if (!chroma) return;
    cancelAnimationFrame(chroma.raf);
    cancelAnimationFrame(chroma.redraw);
    if (chroma.vfc) chroma.video.cancelVideoFrameCallback(chroma.vfc);
    chroma.canvas.remove();
    chroma.video.removeEventListener("seeked", redrawChroma);
    chroma.video.removeEventListener("loadeddata", redrawChroma);
    chroma.video.classList.remove("chroma-source");
    chroma = null;
  }

  function lightboxVideo() {
    return $("lightbox-media").querySelector("video");
  }

  chromaOn.addEventListener("change", () => {
    saveChromaPrefs();
    const video = lightboxVideo();
    if (chromaOn.checked && video) startChroma(video);
    else stopChroma();
  });
  chromaTolerance.addEventListener("input", () => {
    $("chroma-tolerance-value").textContent = chromaTolerance.value;
    redrawChroma();
    saveChromaPrefs();
  });
  chromaDespill.addEventListener("change", () => {
    redrawChroma();
    saveChromaPrefs();
  });
  for (const btn of document.querySelectorAll("#chroma-controls .swatch")) {
    btn.addEventListener("click", () => {
      chromaBg = btn.dataset.bg;
      for (const other of document.querySelectorAll("#chroma-controls .swatch")) {
        other.setAttribute("aria-pressed", String(other === btn));
      }
      if (chroma) chroma.canvas.className = `chroma-canvas bg-${chromaBg}`;
    });
  }

  function setupChroma(video) {
    const prefs = readChromaPrefs();
    chromaOn.checked = prefs.on;
    chromaTolerance.value = String(prefs.tolerance);
    $("chroma-tolerance-value").textContent = chromaTolerance.value;
    chromaDespill.checked = prefs.despill;
    chromaMessage("");
    $("chroma-controls").hidden = false;
    if (prefs.on) startChroma(video);
  }

  // opts (phrase hits): { start: seconds to seek to, caption: line shown under the media }.
  function openLightbox(asset, opts) {
    const start = opts && typeof opts.start === "number" ? Math.max(0, opts.start) : 0;
    const caption = $("lightbox-caption");
    caption.textContent = (opts && opts.caption) || "";
    caption.hidden = !caption.textContent;
    lightboxAsset = asset;
    lightboxCard = (opts && opts.card) || el("div");
    lightboxClip = (opts && opts.clip) || null;
    const dlBtn = $("lightbox-download");
    dlBtn.textContent = lightboxClip ? "Download clip" : "Download";
    dlBtn.title = lightboxClip ? `Download ${lightboxClip} s` : "";
    $("lightbox-mp3").hidden = Boolean(lightboxClip) || !(asset.kind === "video" && asset.provider === "youtube");
    setRightsWarning(dlBtn, asset);
    setRightsWarning($("lightbox-mp3"), asset);
    paintState(lightbox, cardViews.get(lightboxCard) || null);
    stopChroma();
    $("chroma-controls").hidden = true;
    $("lightbox-title").textContent = asset.title;
    const media = $("lightbox-media");
    media.classList.remove("has-audio");
    const still = () => {
      if (!asset.preview_url) return placeholder(asset.kind);
      const img = el("img", { src: previewUrl(asset), alt: asset.title });
      img.addEventListener("error", () => img.replaceWith(placeholder(asset.kind)));
      return img;
    };
    if (isYouTube(asset)) {
      media.replaceChildren(el("iframe", {
        class: "embed",
        src: `https://www.youtube-nocookie.com/embed/${asset.id}?autoplay=1${start ? `&start=${Math.floor(start)}` : ""}`,
        title: asset.title,
        allow: "autoplay; encrypted-media",
        allowfullscreen: true,
        referrerpolicy: "strict-origin-when-cross-origin",
      }));
    } else if (asset.kind === "video" && hasMedia(asset)) {
      const video = el("video", { controls: true, autoplay: true, preload: "metadata", src: mediaUrl(asset) });
      if (asset.preview_url) video.poster = previewUrl(asset);
      if (start) {
        video.addEventListener("loadedmetadata", () => { video.currentTime = start; }, { once: true });
      }
      media.replaceChildren(video);
      setupChroma(video);
    } else if (isAudio(asset) && hasMedia(asset)) {
      media.classList.add("has-audio");
      media.replaceChildren(el("audio", { controls: true, autoplay: true, src: mediaUrl(asset) }), still());
    } else {
      media.replaceChildren(still());
    }
    const lic = $("lightbox-license");
    lic.replaceChildren(renderRightsBadge(asset), " ", el("span", { class: "badge provider", text: asset.provider }));
    const credit = creditChip(asset);
    if (credit) lic.append(" ", credit);
    const rightsLine = $("lightbox-rights");
    const rights = asset.rights;
    if (rights && rights.label) {
      rightsLine.replaceChildren(el("strong", { text: rights.label }), rights.detail ? ` ${rights.detail}` : "");
      rightsLine.className = `lightbox-rights rights-${RIGHTS_LEVELS.includes(rights.level) ? rights.level : "unknown"}`;
      rightsLine.hidden = false;
    } else {
      rightsLine.replaceChildren();
      rightsLine.hidden = true;
    }
    $("lightbox-attribution").textContent = asset.attribution_text || "(no attribution text)";
    const src = $("lightbox-source");
    if (/^https?:\/\//.test(asset.source_url)) {
      src.href = asset.source_url;
      src.hidden = false;
    } else {
      src.removeAttribute("href");
      src.hidden = true;
    }
    if (!lightbox.open) lightbox.showModal();
    $("lightbox-close").focus();
  }

  function closeLightbox() {
    if (lightbox.open) lightbox.close();
  }

  lightbox.addEventListener("close", () => {
    stopChroma();
    $("chroma-controls").hidden = true;
    const media = $("lightbox-media");
    for (const node of media.querySelectorAll("video, audio")) {
      node.pause();
      node.removeAttribute("src");
      node.load();
    }
    for (const frame of media.querySelectorAll("iframe")) frame.src = "about:blank";
    media.replaceChildren();
    media.classList.remove("has-audio");
    lightboxAsset = null;
    lightboxCard = null;
    lightboxClip = null;
  });
  lightbox.addEventListener("click", (ev) => {
    if (ev.target === lightbox) closeLightbox();
  });
  $("lightbox-close").addEventListener("click", closeLightbox);
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (lightbox.open) {
      ev.preventDefault();
      closeLightbox();
    } else if (!$("downloads-drawer").hidden) {
      toggleDrawer(false);
    }
  });
  $("lightbox-download").addEventListener("click", () => {
    if (lightboxAsset) startDownload(lightboxAsset, false, lightboxCard, lightboxClip || undefined);
  });
  $("lightbox-mp3").addEventListener("click", () => {
    if (lightboxAsset) startDownload(lightboxAsset, true, lightboxCard);
  });
  $("lightbox-copy").addEventListener("click", async () => {
    if (!lightboxAsset) return;
    const text = lightboxAsset.attribution_text || "";
    try {
      await navigator.clipboard.writeText(text);
      toast("Attribution copied");
    } catch (_) {
      const range = document.createRange();
      range.selectNodeContents($("lightbox-attribution"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast("Copy blocked by the browser; text selected, press Ctrl+C", true);
    }
  });

  /* ---------- downloads ---------- */

  const tracked = new Map(); // job_id -> { card, timer }
  let jobsCache = [];

  function setCardState(card, kind, ...children) {
    const state = card.querySelector(".card-state");
    state.className = kind ? `card-state ${kind}` : "card-state";
    state.replaceChildren(...children);
    state.hidden = children.length === 0;
  }

  function setCardBusy(card, busy) {
    for (const btn of card.querySelectorAll(".card-actions button")) btn.disabled = busy;
  }

  function progressBar(progress) {
    const wrap = el("div", { class: progress === null || progress === undefined ? "progress indeterminate" : "progress", role: "progressbar", "aria-label": "Download progress" });
    const bar = el("div", { class: "bar" });
    if (typeof progress === "number") {
      bar.style.transform = `scaleX(${progress})`;
      wrap.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
      wrap.setAttribute("aria-valuemin", "0");
      wrap.setAttribute("aria-valuemax", "100");
    }
    wrap.append(bar);
    return wrap;
  }

  const cardViews = new WeakMap(); // card -> last download view, so the lightbox can mirror it

  function jobLabel(job) {
    if (job.status === "queued") return "Queued…";
    if (job.stage === "postprocessing") return "Processing…";
    if (typeof job.progress === "number") return `Downloading ${Math.round(job.progress * 100)}%`;
    return "Downloading…";
  }

  // Paint a view ({ starting } | { error } | { job } | null) into a card-like target.
  function paintState(target, view) {
    if (!view) {
      setCardBusy(target, false);
      setCardState(target, "");
      return;
    }
    if (view.starting) {
      setCardBusy(target, true);
      setCardState(target, "", el("span", { text: "Starting…" }), progressBar(null));
      return;
    }
    if (view.error) {
      setCardBusy(target, false);
      setCardState(target, "error", el("span", { text: view.error }));
      return;
    }
    const job = view.job;
    if (job.status === "done") {
      setCardBusy(target, false);
      const btn = el("button", { type: "button", class: "small", text: "Show in folder" });
      btn.addEventListener("click", () => openPath(job.path));
      const saved = job.history_id == null
        ? el("span", { text: "Saved", title: job.path })
        : makeDraggable(el("span", { class: "drag-handle" }, gripIcon(), "Saved"), job.history_id, job.path);
      setCardState(target, "ok", saved, btn);
    } else if (job.status === "error") {
      setCardBusy(target, false);
      setCardState(target, "error", el("span", { text: job.error || "Download failed" }));
    } else {
      setCardBusy(target, true);
      const wrapper = el("div", { class: "state-progress" }, el("span", { text: jobLabel(job) }), progressBar(job.progress));
      setCardState(target, "", wrapper);
    }
  }

  function setView(card, view) {
    if (!card) return;
    cardViews.set(card, view);
    if (card.isConnected) paintState(card, view);
    if (lightbox.open && lightboxCard === card) paintState(lightbox, view);
  }

  function showJobOnCard(card, job) {
    setView(card, { job });
  }

  async function startDownload(asset, mp3, card, clip) {
    setView(card, { starting: true });
    let accepted;
    try {
      // Always send quality ("best" too) so the UI choice overrides [download] quality.
      const body = { ref: asset.ref, mp3, quality: $("quality").value };
      if (clip) {
        body.clip = clip;
        // Empty: server default ([download] clip_max_wall_s or 300 s); 0: no limit.
        const limit = $("clip-max-wall").value.trim();
        if (limit !== "") body.max_wall_s = Number(limit);
      }
      accepted = await api("POST", "/api/download", body);
    } catch (err) {
      setView(card, { error: err.message });
      return;
    }
    rememberJob(accepted.job_id, { title: asset.title, mp3, clip: clip || null });
    const entry = { card, timer: 0 };
    tracked.set(accepted.job_id, entry);
    pollJob(accepted.job_id);
    refreshJobs();
  }

  async function pollJob(jobId) {
    const entry = tracked.get(jobId);
    if (!entry) return;
    let job;
    try {
      job = await api("GET", `/api/jobs/${encodeURIComponent(jobId)}`);
    } catch (err) {
      showJobOnCard(entry.card, { status: "error", error: `Lost track of job: ${err.message}` });
      tracked.delete(jobId);
      refreshJobs();
      return;
    }
    showJobOnCard(entry.card, job);
    if (job.status === "done" || job.status === "error") {
      tracked.delete(jobId);
      if (job.status === "done") toast(`Saved ${baseName(job.path)}`, false, job.path);
      refreshJobs();
      return;
    }
    entry.timer = setTimeout(() => pollJob(jobId), POLL_MS);
  }

  let jobsTimer = 0;

  async function refreshJobs() {
    clearTimeout(jobsTimer);
    try {
      jobsCache = await api("GET", "/api/jobs");
    } catch (_) {
      return;
    }
    if (!dragging) renderJobs();
    const active = jobsCache.some((j) => j.status === "queued" || j.status === "running");
    if (active || !$("downloads-drawer").hidden) jobsTimer = setTimeout(refreshJobs, active ? POLL_MS : 3000);
  }

  // Client-side job details the API does not return (title, mp3, clip), kept for the session.
  function readJobMeta() {
    try {
      const meta = JSON.parse(sessionStorage.getItem(JOB_META_KEY) || "{}");
      return meta && typeof meta === "object" ? meta : {};
    } catch (_) {
      return {};
    }
  }

  const jobMeta = readJobMeta();

  function rememberJob(jobId, meta) {
    jobMeta[jobId] = meta;
    try {
      sessionStorage.setItem(JOB_META_KEY, JSON.stringify(jobMeta));
    } catch (_) { /* storage unavailable: titles last until reload */ }
  }

  function firstLine(text) {
    return String(text || "").split(/\r?\n/)[0].trim();
  }

  function renderJobs() {
    const active = jobsCache.filter((j) => j.status === "queued" || j.status === "running").length;
    const badge = $("downloads-badge");
    badge.textContent = String(active);
    badge.hidden = active === 0;
    $("downloads-toggle").setAttribute("aria-label", `Downloads, ${active} active`);

    // Identical queued/running downloads of the same asset collapse into one entry with a count.
    const groups = [];
    const byKey = new Map();
    for (const job of jobsCache) {
      const meta = jobMeta[job.job_id] || {};
      const live = job.status === "queued" || job.status === "running";
      const key = live ? [job.kind, job.ref || job.url, meta.mp3 ? 1 : 0, meta.clip || ""].join("|") : null;
      if (key && byKey.has(key)) {
        byKey.get(key).count += 1;
        continue;
      }
      const group = { job, meta, count: 1 };
      if (key) byKey.set(key, group);
      groups.push(group);
    }

    const list = $("downloads-list");
    $("downloads-empty").hidden = jobsCache.length > 0;
    list.replaceChildren(...groups.map(({ job, meta, count }) => {
      const ref = job.ref || job.url || job.job_id;
      // The server's title when it sends one; else the one remembered when the job started.
      const fullTitle = job.title || meta.title || "";
      const title = firstLine(fullTitle) || (job.kind === "keyout" ? "Key out" : ref);
      const heading = job.kind === "keyout" && fullTitle ? `Key out: ${title}` : title;
      const kindNote = meta.mp3 ? " · MP3" : meta.clip ? ` · clip ${meta.clip}` : "";
      const li = el("li", null,
        el("span", { class: "job-title", title: fullTitle || ref, text: count > 1 ? `${heading} ×${count}` : heading }),
        title !== ref || kindNote
          ? el("span", { class: "job-ref", title: job.ref || job.url || "", text: `${ref}${kindNote}` })
          : null);
      if (job.status === "done") {
        const btn = el("button", { type: "button", class: "small", text: "Show in folder" });
        btn.addEventListener("click", () => openPath(job.path));
        const status = el("span", { class: "job-status", title: job.path || "", text: `Saved: ${baseName(job.path)}` });
        if (job.history_id != null) status.prepend(gripIcon());
        li.append(status, btn);
        makeDraggable(li, job.history_id, job.path);
      } else if (job.status === "error") {
        li.append(el("span", { class: "job-status error", text: job.error || "Download failed" }));
      } else {
        li.append(el("span", { class: "job-status", text: jobLabel(job) }), progressBar(job.progress));
      }
      return li;
    }));
  }

  function toggleDrawer(open) {
    const drawer = $("downloads-drawer");
    const show = open === undefined ? drawer.hidden : open;
    // Closing while focus is inside the drawer: hand focus back to the toggle.
    if (!show && drawer.contains(document.activeElement)) $("downloads-toggle").focus();
    drawer.hidden = !show;
    $("downloads-toggle").setAttribute("aria-expanded", String(show));
    if (show) refreshJobs();
  }

  $("downloads-toggle").addEventListener("click", () => toggleDrawer());
  $("downloads-close").addEventListener("click", () => toggleDrawer(false));

  /* ---------- history ---------- */

  const KEYOUT_EXT = /\.(mp4|webm|mkv|mov)$/i;
  const KEYED_EXT = /\.key\.webm$/i;

  async function loadHistory() {
    const status = $("history-status");
    status.textContent = "Loading…";
    let rows;
    try {
      rows = await api("GET", "/api/history?limit=100");
    } catch (err) {
      status.textContent = "";
      toast(`Could not load history: ${err.message}`, true);
      return;
    }
    status.textContent = rows.length ? `${rows.length} download${rows.length === 1 ? "" : "s"}` : "No downloads yet.";
    $("history-body").replaceChildren(...rows.map((r) => {
      const pathName = el("span", { class: "path-name", title: r.path, text: baseName(r.path) });
      const folderBtn = el("button", { type: "button", class: "small", text: "Show in folder", title: r.path });
      folderBtn.addEventListener("click", () => openPath(r.path));
      const actions = el("td", { class: "row-actions" }, folderBtn);
      // A .key.webm is already keyed: keying it again makes no sense.
      if (KEYOUT_EXT.test(r.path) && !KEYED_EXT.test(r.path)) {
        const keyBtn = el("button", { type: "button", class: "small", text: "Key out", title: "Export a transparent WebM (green screen removed) next to this file" });
        keyBtn.addEventListener("click", () => startKeyOut(r.id, keyBtn, r.title));
        actions.append(keyBtn);
      }
      return makeDraggable(el("tr", null,
        el("td", { class: "when", text: formatWhen(r.fetched_at) }),
        el("td", { text: r.provider }),
        el("td", { text: r.title }),
        el("td", null, el("div", { class: "path-cell" }, gripIcon(), pathName)),
        el("td", { text: r.license }),
        actions), r.id, r.path);
    }));
  }

  async function startKeyOut(historyId, btn, title) {
    btn.disabled = true;
    let job;
    try {
      job = await api("POST", `/api/library/${encodeURIComponent(historyId)}/keyout`, { tolerance: readChromaPrefs().tolerance });
    } catch (err) {
      btn.disabled = false;
      toast(`Key out failed: ${err.message}`, true);
      return;
    }
    rememberJob(job.job_id, { title: title || "", mp3: false, clip: null });
    toggleDrawer(true);
    pollKeyOut(job.job_id, btn);
  }

  async function pollKeyOut(jobId, btn) {
    let job;
    try {
      job = await api("GET", `/api/jobs/${encodeURIComponent(jobId)}`);
    } catch (err) {
      btn.disabled = false;
      toast(`Lost track of job: ${err.message}`, true);
      return;
    }
    if (job.status === "done") {
      toast(`Saved ${baseName(job.path)}`, false, job.path);
      refreshJobs();
      loadHistory();
      return;
    }
    if (job.status === "error") {
      btn.disabled = false;
      toast(job.error || "Key out failed", true);
      refreshJobs();
      return;
    }
    setTimeout(() => pollKeyOut(jobId, btn), POLL_MS);
  }

  $("history-refresh").addEventListener("click", loadHistory);

  /* ---------- settings ---------- */

  async function loadSettings() {
    let s;
    try {
      s = await api("GET", "/api/settings");
    } catch (err) {
      toast(`Could not load settings: ${err.message}`, true);
      return;
    }
    $("library-dir").value = s.library_dir;
    $("config-path").value = s.config_path;
    const list = $("keys-list");
    // Assist keys have their own rows in the Assist block.
    const ids = Object.keys(s.keys).filter((id) => !ASSIST_KEY_IDS.includes(id));
    if (!ids.length) {
      list.replaceChildren(el("p", { class: "muted", text: "No providers need a key." }));
    } else {
      list.replaceChildren(...ids.map((id) => keyRow(id, s.keys[id])));
    }
    applySafeSetting(s.search);
    renderAssistSettings(s.assist || null);
    renderDoctor();
    refreshAssistStatus();
  }

  /* settings: search (safe search) */

  // Missing `search` (older server): leave safe search shown as on, the server default.
  function applySafeSetting(search) {
    safeSetting = !(search && search.safe === false);
    $("safe-search").checked = safeSetting;
    $("empty-safe").hidden = !safeSetting;
  }

  // Boot: the setting drives the empty-state phrase and the few-results chip before Settings opens.
  async function loadSafeSetting() {
    try {
      const s = await api("GET", "/api/settings");
      applySafeSetting(s.search);
    } catch (_) { /* keep the default (on) */ }
  }

  $("safe-search").addEventListener("change", async () => {
    const box = $("safe-search");
    const safe = box.checked;
    box.disabled = true;
    try {
      await api("PUT", "/api/settings/search", { safe });
      toast(safe ? "Safe search on" : "Safe search off");
    } catch (err) {
      toast(`Could not save safe search: ${err.message}`, true);
    } finally {
      await loadSafeSetting(); // re-read: the checkbox shows what the server has
      box.disabled = false;
    }
  });

  /* settings: assist */

  const ASSIST_KEY_IDS = ["anthropic", "openai"];
  const ASSIST_BACKEND_IDS = ["claude_cli", "anthropic", "openai"];
  const ASSIST_EXPLAIN = {
    claude_cli: ["Claude Code: uses your existing Claude Code login (no key needed)"],
    anthropic: ["Anthropic API: key or ", el("code", { text: "ant auth login" }), " profile; install ",
      el("code", { text: "clipforage[assist]" })],
    openai: ["OpenAI: API key (OpenAI offers no sign-in for third-party apps)"],
  };

  // Off-by-default opt-in: the controls stay visible but de-emphasised while assist is off.
  function applyAssistEnabled(enabled) {
    $("assist-enabled").checked = enabled;
    $("assist-controls").classList.toggle("is-off", !enabled);
  }

  function renderAssistSettings(assist) {
    const assistForm = $("assist-form");
    const keys = $("assist-keys");
    if (!assist) {
      assistForm.hidden = true;
      $("assist-enabled").disabled = true;
      applyAssistEnabled(false);
      keys.replaceChildren(el("p", { class: "muted", text: "This server does not report assist settings yet." }));
      return;
    }
    assistForm.hidden = false;
    $("assist-enabled").disabled = false;
    applyAssistEnabled(assist.enabled === true);
    const backend = assist.backend && assist.backend !== "" ? assist.backend : "auto";
    const select = $("assist-backend");
    if (![...select.options].some((o) => o.value === backend)) {
      select.append(el("option", { value: backend, text: backend }));
    }
    select.value = backend;
    $("assist-model").value = assist.model || "";
    for (const id of ASSIST_BACKEND_IDS) $(`assist-${id}-model`).value = assist[`${id}_model`] || "";
    const masked = assist.keys || {};
    keys.replaceChildren(...ASSIST_KEY_IDS.map((id) => keyRow(id, masked[id] || null, refreshAssistStatus)));
  }

  async function refreshAssistStatus() {
    const list = $("assist-status-list");
    const current = $("assist-current");
    const s = await loadAssistStatus();
    if (!s || !Array.isArray(s.backends)) {
      list.replaceChildren(el("li", { class: "muted", text: "Assist status unavailable on this server." }));
      current.hidden = true;
      return;
    }
    if (typeof s.enabled === "boolean" && !$("assist-enabled").disabled) applyAssistEnabled(s.enabled);
    list.replaceChildren(...s.backends.map((b) => {
      const explain = ASSIST_EXPLAIN[b.id];
      return el("li", null,
        el("span", { class: b.available ? "status-dot ok" : "status-dot", "aria-hidden": "true" }),
        el("div", { class: "assist-backend" },
          el("div", { class: "assist-backend-line" },
            el("strong", { text: backendName(b.id) }),
            el("span", { class: b.available ? "assist-state ok" : "assist-state", text: b.available ? "Available" : "Not set up" }),
            b.model ? el("span", { class: "muted", text: b.model }) : null),
          b.detail ? el("span", { class: "detail", text: b.detail }) : null,
          explain ? el("span", { class: "assist-explain muted" }, ...explain.map((n) => (n instanceof Node ? n.cloneNode(true) : n))) : null));
    }));
    const configured = s.configured || "auto";
    current.textContent = configured === "auto"
      ? (s.auto_pick ? `Using: auto (currently ${backendName(s.auto_pick)})` : "Using: auto (no backend available)")
      : `Using: ${backendName(configured)}`;
    current.hidden = false;
  }

  // PUT the whole assist block (enabled + backend + model) so no field is reset by omission.
  async function saveAssistSettings(btn, message) {
    const backend = $("assist-backend").value;
    const model = $("assist-model").value.trim();
    btn.disabled = true;
    try {
      // Send "auto" and "" as-is: the server treats null as "leave unchanged", so null here
      // would make it impossible to go back to auto or clear the model.
      const body = { enabled: $("assist-enabled").checked, backend, model };
      for (const id of ASSIST_BACKEND_IDS) body[`${id}_model`] = $(`assist-${id}-model`).value.trim();
      const res = await api("PUT", "/api/settings/assist", body);
      if (res && res.assist) renderAssistSettings(res.assist);
      toast(message);
    } catch (err) {
      toast(`Could not save assist settings: ${err.message}`, true);
      if (btn === $("assist-enabled")) applyAssistEnabled(!btn.checked); // server unchanged
    } finally {
      btn.disabled = false;
      refreshAssistStatus();
    }
  }

  $("assist-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveAssistSettings($("assist-form").querySelector("button[type=submit]"), "Assist settings saved");
  });

  $("assist-enabled").addEventListener("change", () => {
    const box = $("assist-enabled");
    applyAssistEnabled(box.checked);
    saveAssistSettings(box, box.checked ? "AI assist enabled" : "AI assist turned off");
  });

  function keyRow(id, masked, onSaved) {
    const inputId = `key-${id}`;
    const mask = el("span", { class: masked ? "key-mask set" : "key-mask", text: masked || "not set" });
    const input = el("input", { id: inputId, type: "password", autocomplete: "off", spellcheck: "false", placeholder: "paste API key" });
    const name = el("label", { class: "key-name", for: inputId }, id);
    const row = el("form", { class: "key-row" });
    const nameCell = el("div", null, name);
    if (KEY_PAGES[id]) {
      nameCell.append(el("a", { class: "key-link", href: KEY_PAGES[id], target: "_blank", rel: "noopener noreferrer", text: "get a key ↗" }));
    }
    const save = el("button", { type: "submit", class: "primary", text: "Save" });
    const remove = el("button", { type: "button", class: "danger", text: "Remove", disabled: !masked });

    async function put(value) {
      save.disabled = true;
      remove.disabled = true;
      try {
        const res = await api("PUT", `/api/settings/keys/${encodeURIComponent(id)}`, { key: value });
        mask.textContent = res.key || "not set";
        mask.className = res.key ? "key-mask set" : "key-mask";
        input.value = "";
        toast(res.key ? `${id} key saved` : `${id} key removed`);
        remove.disabled = !res.key;
        loadProviders();
        if (onSaved) onSaved();
      } catch (err) {
        toast(`Could not update ${id} key: ${err.message}`, true);
        remove.disabled = mask.textContent === "not set";
      } finally {
        save.disabled = false;
      }
    }

    row.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const value = input.value.trim();
      if (!value) {
        toast(`Paste a ${id} key first`, true);
        input.focus();
        return;
      }
      put(value);
    });
    remove.addEventListener("click", () => put(""));
    row.append(nameCell, mask, input, save, remove);
    return row;
  }

  $("library-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const dir = $("library-dir").value.trim();
    try {
      const res = await api("PUT", "/api/settings/library_dir", { dir });
      $("library-dir").value = res.library_dir;
      toast(`Library folder set to ${res.library_dir}`);
    } catch (err) {
      toast(`Could not set library folder: ${err.message}`, true);
    }
  });

  /* ---------- boot ---------- */

  // Skip link: focus whichever result list is showing (phrase mode hides #results).
  $("skip-results").addEventListener("click", (ev) => {
    ev.preventDefault();
    const target = $("results").hidden ? $("phrase-results") : $("results");
    if (target.hidden) $("result-status").scrollIntoView();
    else target.focus();
  });

  const saved = restoreForm();
  applyFiltersLayout();
  updateFiltersCount();
  loadProviders(saved && typeof saved.provider === "string" ? saved.provider : "");
  loadDoctor();
  loadSafeSetting();
  loadAssistStatus(); // once on load: Describe shows "AI assist is off" without calling the API
  refreshJobs();
  $("q").focus();
})();
