// bmk web UI: vanilla JS, no build step. All data is rendered with textContent (titles come from the web).
"use strict";

const TOKEN = document.querySelector('meta[name="bmk-token"]').content;
const APPROXIMATE = new Set(["first_seen_initial", "file_birthtime", "file_mtime"]);
const VIEW_LABELS = {
  inbox: "Inbox",
  kept: "Kept",
  active: "All",
  unprocessed: "Not fetched yet",
  failed: "Fetch failed / blocked",
  date_unknown: "Date unknown",
  resaved: "Deleted but saved again",
  trash: "Trash",
};

const state = {
  panel: "bookmarks",
  view: "inbox",
  q: "",
  source: null, // {source_type, source_ref}
  sort: "saved_at",
  desc: true,
  offset: 0,
  limit: 100,
  items: [],
  total: 0,
  selected: new Set(),
  detailId: null,
  cursor: -1, // keyboard-selected row on this page
  tz: "UTC",
};

// ------------------------------------------------------------------ helpers

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Like node.replaceChildren, but skips null/false (replaceChildren would render them as text).
function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-BMK-Token": TOKEN } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    err.kind = data.kind;
    throw err;
  }
  return data;
}

function fmtDate(iso, source = "exact") {
  if (!iso) return "—";
  const d = new Date(iso);
  const text = new Intl.DateTimeFormat("sv-SE", {
    timeZone: state.tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(d);
  return APPROXIMATE.has(source) ? `~${text}` : text;
}

function fallbackTitle(url) {
  try {
    const u = new URL(url);
    return decodeURIComponent(u.hostname + (u.pathname === "/" ? "" : u.pathname));
  } catch {
    return url;
  }
}

function safeHref(url) {
  return /^https?:\/\//i.test(url || "") ? url : undefined;
}

let toastTimer;
function toast(message, action) {
  const t = $("#toast");
  t.replaceChildren(el("span", {}, message));
  if (action) t.append(el("button", { class: "link", onclick: () => { t.hidden = true; action.run(); } }, action.label));
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), action ? 8000 : 4000);
}

function showError(err) {
  toast(`Error: ${err.message.split("\n")[0]}`);
  console.error(err);
}

function banner(run) {
  const b = $("#banner");
  if (!run || !run.error) {
    b.hidden = true;
    return;
  }
  const [first, ...rest] = run.error.split("\n");
  fill(b,
    el("strong", {}, "Last iCloud Tabs scan failed: "),
    el("span", {}, first),
    rest.length ? el("details", {}, el("summary", {}, "How to fix"), el("pre", {}, rest.join("\n"))) : null,
  );
  b.hidden = false;
}

// ------------------------------------------------------------------ sidebar

async function loadMeta() {
  const params = new URLSearchParams({ view: state.view });
  if (state.source) {
    params.set("source_type", state.source.source_type);
    if (state.source.source_ref) params.set("source_ref", state.source.source_ref);
  }
  const meta = await api("GET", `/api/meta?${params}`);
  state.tz = meta.display_tz || "UTC";
  const views = $("#views");
  views.replaceChildren(
    ...Object.entries(VIEW_LABELS).map(([view, label]) =>
      el("li", {},
        el("a", {
          href: "#",
          class: state.panel === "bookmarks" && state.view === view ? "active" : "",
          onclick: (e) => { e.preventDefault(); selectView(view); },
        }, label, el("span", { class: "count" }, meta.counts[view] ?? "")),
      ),
    ),
  );
  const sources = $("#sources");
  sources.replaceChildren(
    ...meta.sources.map((s) => {
      const active = state.source && state.source.source_type === s.source_type && state.source.source_ref === s.source_ref;
      return el("li", {},
        el("a", {
          href: "#",
          class: active ? "active" : "",
          title: active ? "Click again to show all sources" : `Only ${s.source_ref || s.source_type} (combines with the list above)`,
          onclick: (e) => { e.preventDefault(); selectSource(s); },
        }, s.source_ref || s.source_type, el("span", { class: "count" }, s.bookmarks)),
      );
    }),
  );
  for (const a of document.querySelectorAll("[data-panel]")) {
    a.classList.toggle("active", state.panel === a.dataset.panel);
  }
  banner(meta.last_cloudtabs_run);
}

function showPanel(panel) {
  state.panel = panel;
  for (const p of ["bookmarks", "runs", "health"]) $(`#panel-${p}`).hidden = p !== panel;
  if (panel !== "bookmarks") closeDetail();
}

// The list (Inbox, Kept, …) and the source (a device) are separate filters that combine.
function selectView(view) {
  Object.assign(state, { view, offset: 0 });
  state.selected.clear();
  showPanel("bookmarks");
  refresh();
}

function selectSource(source) {
  const same = state.source && state.source.source_type === source.source_type && state.source.source_ref === source.source_ref;
  Object.assign(state, { source: same ? null : source, offset: 0 });
  state.selected.clear();
  showPanel("bookmarks");
  refresh();
}

// ------------------------------------------------------------------ table

async function loadBookmarks() {
  const params = new URLSearchParams({
    view: state.view, sort: state.sort, desc: state.desc, limit: state.limit, offset: state.offset,
  });
  if (state.q) params.set("q", state.q);
  if (state.source) {
    params.set("source_type", state.source.source_type);
    if (state.source.source_ref) params.set("source_ref", state.source.source_ref);
  }
  const page = await api("GET", `/api/bookmarks?${params}`);
  state.items = page.items;
  state.total = page.total;
  renderRows();
}

const FETCH_LABELS = {
  pending: "Not fetched yet",
  fetched: "Fetched",
  partial: "Fetched (not a web page, or nothing to read)",
  skipped: "Not fetched (the link says what it is)",
  needs_browser: "Blocked: the site wants a real browser or a login",
  failed: "Fetch failed",
};

function fetchMark(item) {
  if (item.fetch_status !== "failed" && item.fetch_status !== "needs_browser") return null;
  return el("span", { class: "fetch-mark", title: `${FETCH_LABELS[item.fetch_status]}${item.fetch_error ? `: ${item.fetch_error}` : ""}` }, " ⚠");
}

function lockMark(item, field) {
  return item.locked_fields.includes(field) ? el("span", { class: "lock", title: "Edited by you; locked against automatic updates" }, " 🔒") : null;
}

function editableCell(item, field, display, ...extra) {
  const td = el("td", { class: `editable ${field}`, title: "Double-click to edit" }, display, lockMark(item, field), ...extra);
  td.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    startEdit(td, item, field);
  });
  return td;
}

function startEdit(td, item, field) {
  if (td.querySelector("input")) return;
  const input = el("input", { type: "text", value: item[field] || "", "aria-label": `Edit ${field}` });
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    if (save && input.value !== (item[field] || "")) {
      try {
        await api("PATCH", `/api/bookmarks/${item.id}`, { [field]: input.value });
        toast(`Saved ${field}`);
      } catch (err) {
        showError(err);
      }
    }
    await refresh();
    if (state.detailId === item.id) await openDetail(item.id);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  td.replaceChildren(input);
  input.focus();
  input.select();
}

const SVG_NS = "http://www.w3.org/2000/svg";
function icon(...paths) {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", width: "15", height: "15", fill: "none", stroke: "currentColor",
    "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "icon" })) svg.setAttribute(k, v);
  for (const d of paths) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
const trashIcon = () => icon("M3 6h18", "M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2", "M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6", "M10 11v6", "M14 11v6");
const keepIcon = () => icon("M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z", "M8 12.5l2.5 2.5L16 9.5");

// Row actions: Keep (Inbox → Kept, or back) and Trash, or Restore when the bookmark is in Trash.
function actionButtons(item) {
  if (item.deleted_at) {
    return [el("button", { class: "link", title: "Restore from Trash", onclick: (e) => { e.stopPropagation(); restoreOne(item.id); } }, "Restore")];
  }
  const kept = item.status === "kept";
  return [
    el("button", {
      class: `link icon-button keep${kept ? " on" : ""}`,
      title: kept ? "Kept. Click to move back to the Inbox" : "Keep (K): move from the Inbox to Kept",
      "aria-label": kept ? "Move back to Inbox" : "Keep",
      "aria-pressed": kept ? "true" : "false",
      onclick: (e) => { e.stopPropagation(); setKept(item.id, !kept); },
    }, keepIcon()),
    el("button", {
      class: "link danger icon-button",
      title: "Move to Trash (D). Undo is available",
      "aria-label": "Move to Trash",
      onclick: (e) => { e.stopPropagation(); deleteOne(item.id); },
    }, trashIcon()),
  ];
}

function renderRows() {
  const tbody = $("#rows");
  if (!state.items.length) {
    const empty = state.q ? "No matches." : state.view === "inbox" ? "Inbox is empty. Everything has been kept or trashed." : "Nothing here yet.";
    tbody.replaceChildren(el("tr", {}, el("td", { colspan: 7, class: "empty" }, empty)));
  } else {
    tbody.replaceChildren(...state.items.map((item) => {
      const href = safeHref(item.url);
      const title = el("span", { class: item.title ? "" : "untitled" }, item.title || fallbackTitle(item.url));
      const open = href
        ? el("a", { href, target: "_blank", rel: "noopener noreferrer", class: "open", title: "Open page", onclick: (e) => e.stopPropagation() }, "↗")
        : null;
      const check = el("input", {
        type: "checkbox",
        checked: state.selected.has(item.id),
        "aria-label": "Select",
        onclick: (e) => {
          e.stopPropagation();
          e.target.checked ? state.selected.add(item.id) : state.selected.delete(item.id);
          renderBulkbar();
        },
      });
      return el("tr", {
        "data-id": item.id,
        class: item.id === state.detailId ? "selected" : "",
        onclick: () => { state.cursor = state.items.indexOf(item); markCursor(); openDetail(item.id); },
      },
        el("td", { class: "check" }, check),
        el("td", { class: "date", title: item.saved_at_source }, fmtDate(item.saved_at, item.saved_at_source), lockMark(item, "saved_at")),
        editableCell(item, "title", title, open, fetchMark(item),
          item.description ? el("div", { class: "desc", title: item.description }, item.description) : null),
        el("td", { class: "domain" }, item.domain || ""),
        el("td", { class: "sources" }, ...item.source_labels.map((s) => el("span", { class: "badge" }, s))),
        editableCell(item, "remarks", item.remarks || ""),
        el("td", { class: "actions" }, ...actionButtons(item)),
      );
    }));
  }
  const end = Math.min(state.offset + state.items.length, state.total);
  $("#pageinfo").textContent = state.total ? `${state.offset + 1}–${end} of ${state.total}` : "0";
  $("#prev").disabled = state.offset === 0;
  $("#next").disabled = end >= state.total;
  for (const th of document.querySelectorAll("th[data-sort]")) {
    th.classList.toggle("sorted", th.dataset.sort === state.sort);
    th.dataset.dir = th.dataset.sort === state.sort ? (state.desc ? "desc" : "asc") : "";
  }
  $("#select-all").checked = state.items.length > 0 && state.items.every((i) => state.selected.has(i.id));
  state.cursor = Math.min(state.cursor, state.items.length - 1);
  markCursor();
  renderBulkbar();
}

function markCursor() {
  const rows = document.querySelectorAll("#rows tr[data-id]");
  rows.forEach((tr, i) => tr.classList.toggle("cursor", i === state.cursor));
  rows[state.cursor]?.scrollIntoView({ block: "nearest" });
}

// Keyboard triage: ↓/↑ move, K keeps, D trashes, O opens the page, Enter shows details.
function triageKey(e) {
  if (state.panel !== "bookmarks" || e.metaKey || e.ctrlKey || e.altKey) return false;
  const item = state.items[state.cursor];
  switch (e.key) {
    case "ArrowDown":
    case "ArrowUp":
      state.cursor = Math.max(0, Math.min(state.items.length - 1, state.cursor + (e.key === "ArrowDown" ? 1 : -1)));
      markCursor();
      if (!$("#detail").hidden && state.items[state.cursor]) openDetail(state.items[state.cursor].id);
      return true;
    case "k":
    case "K":
      if (item && !item.deleted_at) setKept(item.id, item.status !== "kept");
      return Boolean(item);
    case "d":
    case "D":
    case "Delete":
    case "Backspace":
      if (item && !item.deleted_at) deleteOne(item.id);
      return Boolean(item);
    case "o":
    case "O": {
      const href = item && safeHref(item.url);
      if (href) window.open(href, "_blank", "noopener,noreferrer");
      return Boolean(href);
    }
    case "Enter":
      if (item) openDetail(item.id);
      return Boolean(item);
    default:
      return false;
  }
}

function renderBulkbar() {
  const bar = $("#bulkbar");
  bar.hidden = state.selected.size === 0;
  $("#bulkcount").textContent = `${state.selected.size} selected`;
  const inTrash = state.view === "trash" || state.view === "resaved";
  for (const b of bar.querySelectorAll("[data-bulk]")) {
    const restore = b.dataset.bulk === "restore";
    if (b.dataset.bulk !== "clear") b.hidden = inTrash ? !restore : restore;
  }
}

async function bulk(spec) {
  if (spec === "clear") {
    state.selected.clear();
    return renderRows();
  }
  const ids = [...state.selected];
  const [action, status] = spec.split(":");
  if (action === "delete" && ids.length > 1 && !window.confirm(`Move ${ids.length} bookmarks to Trash?`)) return;
  try {
    await api("POST", "/api/bookmarks/bulk", { ids, action, status });
    state.selected.clear();
    if (action === "delete") {
      toast(`${ids.length} moved to Trash`, { label: "Undo", run: () => api("POST", "/api/bookmarks/bulk", { ids, action: "restore" }).then(refresh) });
    } else if (action === "refetch") {
      toast(`${ids.length} queued to fetch again`);
    } else if (spec === "status:kept") {
      toast(`${ids.length} kept`, { label: "Undo", run: () => api("POST", "/api/bookmarks/bulk", { ids, action: "status", status: "inbox" }).then(refresh) });
    } else {
      toast(`${ids.length} updated`);
    }
  } catch (err) {
    showError(err);
  }
  await refresh();
}

async function setKept(id, kept) {
  try {
    await api("PATCH", `/api/bookmarks/${id}`, { status: kept ? "kept" : "inbox" });
    toast(kept ? "Kept" : "Moved back to the Inbox", { label: "Undo", run: () => setKept(id, !kept) });
  } catch (err) {
    showError(err);
  }
  await refresh();
  await followDetail(id);
}

// After a triage action: keep showing the bookmark if it is still listed, otherwise move on to the next one.
async function followDetail(id) {
  if ($("#detail").hidden) return;
  const next = state.items.some((i) => i.id === id) ? id : state.items[state.cursor]?.id;
  return next ? openDetail(next) : closeDetail();
}

async function deleteOne(id) {
  try {
    await api("DELETE", `/api/bookmarks/${id}`);
    toast("Moved to Trash", { label: "Undo", run: () => restoreOne(id) });
  } catch (err) {
    showError(err);
  }
  await refresh();
  await followDetail(id);
}

async function restoreOne(id) {
  try {
    await api("POST", `/api/bookmarks/${id}/restore`);
    toast("Restored");
  } catch (err) {
    showError(err);
  }
  await refresh();
}

// ------------------------------------------------------------------ detail pane

function closeDetail() {
  state.detailId = null;
  $("#detail").hidden = true;
}

function row(label, ...value) {
  return el("div", { class: "kv" }, el("div", { class: "k" }, label), el("div", { class: "v" }, ...value));
}

function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function openDetail(id) {
  state.detailId = id;
  let b;
  try {
    b = await api("GET", `/api/bookmarks/${id}`);
  } catch (err) {
    return showError(err);
  }
  // Highlight in place; re-rendering the table here would cancel an inline edit started by a double-click.
  for (const tr of document.querySelectorAll("#rows tr")) tr.classList.toggle("selected", tr.dataset.id === String(id));
  const pane = $("#detail");
  const href = safeHref(b.url);
  const dateInput = el("input", { type: "datetime-local", value: toLocalInput(b.saved_at), "aria-label": "Saved at" });
  const remarks = el("textarea", { rows: 4, "aria-label": "Remarks" }, b.remarks || "");
  const save = async (fields) => {
    try {
      await api("PATCH", `/api/bookmarks/${id}`, fields);
      toast("Saved");
      await refresh();
      await openDetail(id);
    } catch (err) {
      showError(err);
    }
  };
  fill(pane,
    el("div", { class: "detail-head" },
      el("h2", {}, b.title || fallbackTitle(b.url)),
      el("button", { class: "link", "aria-label": "Close", onclick: closeDetail }, "✕"),
    ),
    el("div", { class: "detail-actions" },
      href ? el("a", { class: "button", href, target: "_blank", rel: "noopener noreferrer" }, "Open") : null,
      el("button", { onclick: () => navigator.clipboard.writeText(b.url).then(() => toast("URL copied")) }, "Copy URL"),
      b.deleted_at ? null
        : b.status === "kept"
          ? el("button", { class: "keep on", title: "Move back to the Inbox", onclick: () => setKept(id, false) }, keepIcon(), " Kept")
          : el("button", { class: "keep", onclick: () => setKept(id, true) }, keepIcon(), " Keep"),
      b.deleted_at
        ? el("button", { onclick: () => restoreOne(id).then(() => openDetail(id)) }, "Restore")
        : el("button", { class: "danger", onclick: () => deleteOne(id) }, trashIcon(), " Move to Trash"),
    ),
    b.deleted_at ? el("p", { class: "warn" }, `In Trash since ${fmtDate(b.deleted_at)}. It stays deleted even if the tab is seen again.`) : null,
    previewCard(b, href),
    row("URL", el("a", { href, target: "_blank", rel: "noopener noreferrer" }, b.url)),
    b.url_canonical !== b.url ? row("Canonical", b.url_canonical) : null,
    b.aliases.length > 1 ? row("Aliases", el("ul", {}, ...b.aliases.map((a) => el("li", {}, `${a.kind}: ${a.url_canonical}`)))) : null,
    row("Saved at",
      el("span", {}, fmtDate(b.saved_at, b.saved_at_source), " ", el("span", { class: "muted" }, `(${b.saved_at_source})`)),
      el("div", { class: "inline" }, dateInput,
        el("button", { onclick: () => dateInput.value && save({ saved_at: new Date(dateInput.value).toISOString() }) }, "Set")),
    ),
    row("Fetch", fetchInfo(b)),
    row("Locked", b.locked_fields.length ? b.locked_fields.join(", ") : "—"),
    row("Remarks", remarks, el("button", { onclick: () => save({ remarks: remarks.value }) }, "Save remarks")),
    el("h3", {}, `Sources (${b.sources.length})`),
    el("table", { class: "mini" },
      el("thead", {}, el("tr", {}, el("th", {}, "Source"), el("th", {}, "Device / file"), el("th", {}, "First seen"), el("th", {}, "Last seen"))),
      el("tbody", {}, ...b.sources.map((s) => el("tr", {},
        el("td", {}, s.source_type), el("td", {}, s.source_ref || "—"),
        el("td", {}, fmtDate(s.first_seen_at)), el("td", {}, fmtDate(s.last_seen_at)),
      ))),
    ),
    b.events.length ? el("h3", {}, "History") : null,
    b.events.length ? el("ul", { class: "events" }, ...b.events.map((e) =>
      el("li", {}, `${fmtDate(e.at)} · ${e.actor} · ${e.kind}`, e.data_json ? el("code", {}, ` ${e.data_json}`) : null))) : null,
  );
  pane.hidden = false;
}

function previewCard(b, href) {
  const img = safeHref(b.image_url)
    ? el("img", { class: "preview-img", src: b.image_url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" })
    : null;
  if (img) img.addEventListener("error", () => img.remove());
  const byline = [b.site_name, b.author, b.published_at && b.published_at.slice(0, 10)].filter(Boolean).join(" · ");
  const text = b.content_text || "";
  const excerpt = text.length > 600 ? `${text.slice(0, 600).trimEnd()}…` : text;
  const body = el("div", { class: "preview-text" }, excerpt);
  const more = text.length > 600
    ? el("button", { class: "link", onclick: (e) => {
      const full = body.classList.toggle("full");
      body.textContent = full ? text : excerpt;
      e.target.textContent = full ? "Show less" : "Show full text";
    } }, "Show full text")
    : null;
  const frame = el("div", { class: "live" });
  const blocked = b.frame_ok === 0;
  const live = href
    ? el("button", {
      disabled: blocked,
      title: blocked ? "This site doesn't allow being shown inside another page. Use Open." : "Load the real page here (counts as a visit to the site)",
      onclick: (e) => {
        if (frame.firstChild) {
          frame.replaceChildren();
          e.target.textContent = "Live preview";
          return;
        }
        fill(frame,
          el("iframe", { src: href, title: "Live preview", loading: "lazy", referrerpolicy: "no-referrer",
            sandbox: "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox" }),
          el("p", { class: "muted small" }, "Blank? The site may refuse to be shown here. Use Open."));
        e.target.textContent = "Hide live preview";
      },
    }, blocked ? "Live preview unavailable" : "Live preview")
    : null;
  if (!img && !byline && !b.description && !text && !live) return null;
  return el("section", { class: "preview" },
    img,
    byline ? el("div", { class: "byline" }, byline) : null,
    b.description ? el("p", { class: "summary" }, b.description) : null,
    text ? body : null,
    el("div", { class: "inline" }, more, live),
    frame,
  );
}

function fetchInfo(b) {
  const label = FETCH_LABELS[b.fetch_status] || b.fetch_status;
  const job = b.job;
  const parts = [el("span", { class: `fetch-${b.fetch_status}` }, label)];
  if (b.last_fetched_at) parts.push(el("span", { class: "muted" }, ` · ${fmtDate(b.last_fetched_at)}`));
  if (b.http_status && b.http_status !== 200) parts.push(el("span", { class: "muted" }, ` · HTTP ${b.http_status}`));
  if (b.fetch_error) parts.push(el("div", { class: "muted small" }, b.fetch_error));
  if (job && job.state === "retry_wait") parts.push(el("div", { class: "muted small" }, `Retry ${job.attempts + 1} after ${fmtDate(job.next_run_at)}`));
  const button = el("button", {
    onclick: async (e) => {
      e.target.disabled = true;
      e.target.textContent = "Fetching…";
      try {
        const result = await api("POST", `/api/bookmarks/${b.id}/refetch`);
        toast(result.merged_from ? "Same page as an existing bookmark: merged into it" : `Fetch: ${FETCH_LABELS[result.fetch_status] || result.fetch_status}`);
        await refresh();
        await openDetail(result.id);
      } catch (err) {
        showError(err);
        e.target.disabled = false;
        e.target.textContent = "Fetch again";
      }
    },
  }, b.fetch_status === "pending" ? "Fetch now" : "Fetch again");
  return [...parts, el("div", { class: "inline" }, button)];
}

// ------------------------------------------------------------------ runs and health panels

async function loadRuns() {
  const panel = $("#panel-runs");
  const runs = await api("GET", "/api/runs?limit=50");
  panel.replaceChildren(
    el("h2", {}, "Import runs"),
    el("table", { class: "grid" },
      el("thead", {}, el("tr", {}, ...["Run", "Source", "Started", "Seen", "New", "Matched", "Unchanged", "Skipped/Invalid", "Suppressed", "Error"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...runs.map((r) => el("tr", { class: r.error ? "failed" : "", onclick: () => loadRunItems(r.id) },
        el("td", {}, r.id), el("td", {}, r.source_type), el("td", {}, fmtDate(r.started_at)),
        el("td", {}, r.items_seen), el("td", {}, r.items_new), el("td", {}, r.items_matched), el("td", {}, r.items_unchanged),
        el("td", {}, `${r.items_skipped}/${r.items_invalid}`), el("td", {}, r.items_suppressed),
        el("td", { class: "error" }, (r.error || "").split("\n")[0]),
      ))),
    ),
    el("div", { id: "run-items" }),
  );
}

async function loadRunItems(runId) {
  const items = await api("GET", `/api/runs/${runId}/items`);
  const interesting = items.filter((i) => i.outcome !== "unchanged");
  $("#run-items").replaceChildren(
    el("h3", {}, `Run ${runId}: ${items.length} items${interesting.length < items.length ? ` (${items.length - interesting.length} unchanged hidden)` : ""}`),
    el("table", { class: "grid" },
      el("thead", {}, el("tr", {}, el("th", {}, "Outcome"), el("th", {}, "Title"), el("th", {}, "URL"), el("th", {}, "Error"))),
      el("tbody", {}, ...interesting.map((i) => el("tr", { class: `outcome-${i.outcome}`, onclick: () => i.bookmark_id && openBookmarkFromPanel(i.bookmark_id) },
        el("td", {}, el("span", { class: "badge" }, i.outcome)), el("td", {}, i.raw_title || ""),
        el("td", { class: "url" }, i.raw_url || ""), el("td", { class: "error" }, i.error || ""),
      ))),
    ),
  );
}

function openBookmarkFromPanel(id) {
  showPanel("bookmarks");
  loadMeta();
  openDetail(id);
}

async function loadHealth() {
  const panel = $("#panel-health");
  panel.replaceChildren(el("h2", {}, "Health"), el("p", { class: "muted" }, "Checking…"));
  const checks = await api("GET", "/api/doctor");
  panel.replaceChildren(
    el("h2", {}, "Health"),
    el("ul", { class: "checks" }, ...checks.map((c) => el("li", { class: `check-${c.status}` },
      el("span", { class: "pill" }, c.status.toUpperCase()), el("strong", {}, c.name), el("div", {}, c.detail),
      c.fix && c.status !== "ok" ? el("pre", {}, c.fix) : null,
    ))),
    el("h3", {}, "Page fetching"),
    queueLine(),
    el("h3", {}, "Background scan log"),
    el("div", { class: "inline" }, el("button", { onclick: () => loadLog().catch(showError) }, "Show log")),
    el("div", { id: "scan-log" }),
  );
}

function queueLine() {
  const box = el("div", { id: "queue" }, el("p", { class: "muted" }, "…"));
  api("GET", "/api/meta").then((meta) => {
    const q = meta.queue;
    if (!meta.enrich) {
      fill(box, el("p", { class: "muted" }, "Page fetching is off (BMK_ENRICH=false) or its libraries are not installed."));
      return;
    }
    fill(box,
      el("p", {}, `${q.due} to fetch now · ${q.waiting} waiting to retry · ${q.done} done · ${q.dead} failed or blocked`,
        meta.worker_running ? el("span", { class: "muted" }, " · fetching…") : null),
      el("div", { class: "inline" },
        el("button", { onclick: () => api("POST", "/api/process").then((r) => toast(r.started ? "Fetching started" : "No background worker: run bmk process")).catch(showError) }, "Fetch now"),
        el("button", { onclick: () => selectView("failed") }, "Show failed / blocked")),
    );
  }).catch(showError);
  return box;
}

async function loadLog() {
  const log = await api("GET", "/api/log?lines=200");
  const box = $("#scan-log");
  if (!log.exists) {
    fill(box, el("p", { class: "muted" }, `No log yet at ${log.path}. It appears after the first background scan.`));
    return;
  }
  const pre = el("pre", { class: "log" }, log.lines.join("\n") || "(empty)");
  fill(box, el("p", { class: "muted" }, `Last ${log.lines.length} lines of ${log.path}, newest at the bottom.`), pre);
  pre.scrollTop = pre.scrollHeight;
  box.previousElementSibling.querySelector("button").textContent = "Refresh log";
}

// ------------------------------------------------------------------ wiring

async function refresh() {
  try {
    await loadMeta();
    if (state.panel === "bookmarks") await loadBookmarks();
    if (state.panel === "runs") await loadRuns();
  } catch (err) {
    showError(err);
  }
}

async function scan() {
  const button = $("#scan");
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    const result = await api("POST", "/api/ingest/cloudtabs");
    const c = result.counts;
    toast(`Scan done: ${c.new} new, ${c.matched_exact + c.matched_alias} matched, ${c.unchanged} unchanged`);
  } catch (err) {
    showError(err);
  } finally {
    button.disabled = false;
    button.textContent = "Scan iCloud Tabs";
    await refresh();
  }
}

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    state.offset = 0;
    if (state.panel !== "bookmarks") showPanel("bookmarks");
    refresh();
  }, 250);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
    e.preventDefault();
    $("#search").focus();
  }
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
  if (e.key === "Escape" && !$("#detail").hidden && !typing) closeDetail();
  if (!typing && triageKey(e)) e.preventDefault();
});
$("#scan").addEventListener("click", scan);
$("#prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); refresh(); });
$("#next").addEventListener("click", () => { state.offset += state.limit; refresh(); });
$("#select-all").addEventListener("change", (e) => {
  for (const i of state.items) e.target.checked ? state.selected.add(i.id) : state.selected.delete(i.id);
  renderRows();
});
for (const th of document.querySelectorAll("th[data-sort]")) {
  th.addEventListener("click", () => {
    if (state.sort === th.dataset.sort) state.desc = !state.desc;
    else Object.assign(state, { sort: th.dataset.sort, desc: th.dataset.sort === "saved_at" });
    state.offset = 0;
    refresh();
  });
}
for (const b of document.querySelectorAll("[data-bulk]")) b.addEventListener("click", () => bulk(b.dataset.bulk));
for (const a of document.querySelectorAll("[data-panel]")) {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    showPanel(a.dataset.panel);
    loadMeta();
    (a.dataset.panel === "runs" ? loadRuns() : loadHealth()).catch(showError);
  });
}

refresh();
// Pick up background (launchd) scans without a reload.
setInterval(() => { if (state.panel === "bookmarks" && !document.querySelector("#rows input[type=text]")) loadMeta().catch(() => {}); }, 60000);
