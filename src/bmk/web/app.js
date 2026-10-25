// bmk web UI: vanilla JS, no build step. All data is rendered with textContent (titles come from the web).
"use strict";

const TOKEN = document.querySelector('meta[name="bmk-token"]').content;
const APPROXIMATE = new Set(["first_seen_initial", "file_birthtime", "file_mtime"]);
const VIEW_LABELS = {
  inbox: "Inbox",
  kept: "Kept",
  starred: "Starred",
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
  category: null, // a category id, "none" (uncategorised) or null (all)
  categories: [],
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

// A file source's ref is its full path; show the file name (the path is in the tooltip).
function sourceLabel(ref) {
  return ref && ref.includes("/") ? ref.slice(ref.lastIndexOf("/") + 1) : ref;
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
  state.categories = meta.categories;
  const catLink = (value, label, count, title) => el("li", {},
    el("a", {
      href: "#",
      class: state.category === value ? "active" : "",
      title: state.category === value ? "Click again to show every category" : title,
      onclick: (e) => { e.preventDefault(); selectCategory(value); },
    }, label, el("span", { class: "count" }, count)),
  );
  fill($("#categories"),
    ...meta.categories.map((c) => catLink(c.id, c.name, c.bookmarks || "", c.description || `Only ${c.name}`)),
    meta.categories.length ? catLink("none", "Uncategorised", meta.uncategorised || "", "Bookmarks without a category")
      : el("li", {}, el("a", { href: "#", class: "muted", onclick: (e) => { e.preventDefault(); openPanel("categories"); } }, "Set up categories…")),
  );
  fill($("#bulk-category"), el("option", { value: "" }, "Category…"), el("option", { value: "0" }, "— none —"),
    ...meta.categories.map((c) => el("option", { value: c.id }, c.name)));
  $("#review-count").textContent = meta.pending_changesets || "";
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
        }, sourceLabel(s.source_ref) || s.source_type, el("span", { class: "count" }, s.bookmarks)),
      );
    }),
  );
  for (const a of document.querySelectorAll("[data-panel]")) {
    a.classList.toggle("active", state.panel === a.dataset.panel);
  }
  $("#dup-count").textContent = meta.duplicates || "";
  banner(meta.last_cloudtabs_run);
}

function showPanel(panel) {
  state.panel = panel;
  for (const p of ["bookmarks", "categories", "review", "duplicates", "runs", "health"]) $(`#panel-${p}`).hidden = p !== panel;
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

function selectCategory(category) {
  Object.assign(state, { category: state.category === category ? null : category, offset: 0 });
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
  if (state.category !== null) params.set("category", state.category);
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
const starIcon = () => icon("M12 2.8l2.8 5.8 6.4.9-4.6 4.5 1.1 6.4L12 17.4l-5.7 3 1.1-6.4-4.6-4.5 6.4-.9z");
const keepIcon = () => icon("M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z", "M8 12.5l2.5 2.5L16 9.5");

// Row actions: Keep (Inbox → Kept, or back) and Trash, or Restore when the bookmark is in Trash.
function actionButtons(item) {
  if (item.deleted_at) {
    return [el("button", { class: "link", title: "Restore from Trash", onclick: (e) => { e.stopPropagation(); restoreOne(item.id); } }, "Restore")];
  }
  const kept = item.status === "kept";
  const starred = Boolean(item.starred);
  return [
    el("button", {
      class: `link icon-button star${starred ? " on" : ""}`,
      title: starred ? "Starred. Click to remove the star (stays kept)" : "Star (S): keep it and add it to Starred",
      "aria-label": starred ? "Remove star" : "Star",
      "aria-pressed": starred ? "true" : "false",
      onclick: (e) => { e.stopPropagation(); setStarred(item, !starred); },
    }, starIcon()),
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
    tbody.replaceChildren(el("tr", {}, el("td", { colspan: 8, class: "empty" }, empty)));
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
        el("td", { class: "category" }, categoryBadge(item)),
        el("td", { class: "sources" }, ...item.source_labels.map((s) => el("span", { class: "badge", title: s }, sourceLabel(s)))),
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
    case "s":
    case "S":
      if (item && !item.deleted_at) setStarred(item, !item.starred);
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
    } else if (action === "star") {
      toast(`${ids.length} starred`);
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

async function setStarred(item, starred) {
  // Undo restores the status too, since starring an Inbox bookmark also keeps it.
  const before = { starred: Boolean(item.starred), status: item.status };
  try {
    await api("PATCH", `/api/bookmarks/${item.id}`, { starred });
    toast(starred ? "Starred" : "Star removed", { label: "Undo", run: () =>
      api("PATCH", `/api/bookmarks/${item.id}`, before).then(refresh).then(() => followDetail(item.id)).catch(showError) });
  } catch (err) {
    showError(err);
  }
  await refresh();
  await followDetail(item.id);
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
        : el("button", { class: `star${b.starred ? " on" : ""}`, title: b.starred ? "Remove the star (stays kept)" : "Keep it and add it to Starred",
          onclick: () => setStarred(b, !b.starred) }, starIcon(), b.starred ? " Starred" : " Star"),
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
    b.deleted_at ? null : row("Category",
      categorySelect(b.category_id, (value) => save({ category_id: value || 0 })),
      b.category_assigned_by ? el("span", { class: "muted" }, b.category_assigned_by === "ai" ? " set by AI" : " set by you") : null),
    row("Fetch", fetchInfo(b)),
    row("Locked", b.locked_fields.length ? b.locked_fields.join(", ") : "—"),
    row("Remarks", remarks, el("button", { onclick: () => save({ remarks: remarks.value }) }, "Save remarks")),
    el("h3", {}, `Sources (${b.sources.length})`),
    el("table", { class: "mini" },
      el("thead", {}, el("tr", {}, el("th", {}, "Source"), el("th", {}, "Device / file"), el("th", {}, "First seen"), el("th", {}, "Last seen"))),
      el("tbody", {}, ...b.sources.map((s) => el("tr", {},
        el("td", {}, s.source_type), el("td", { title: s.source_ref }, sourceLabel(s.source_ref) || "—"),
        el("td", {}, fmtDate(s.first_seen_at)), el("td", {}, fmtDate(s.last_seen_at)),
      ))),
    ),
    b.merges.length ? el("h3", {}, "Merges") : null,
    b.merges.length ? el("ul", { class: "events" }, ...b.merges.map((m) => el("li", {},
      `${fmtDate(m.merged_at)} · `,
      m.survivor_id === b.id
        ? el("span", {}, el("a", { href: "#", onclick: (e) => { e.preventDefault(); openDetail(m.loser_id); } }, `#${m.loser_id}`), " merged into this")
        : el("span", {}, "merged into ", el("a", { href: "#", onclick: (e) => { e.preventDefault(); openDetail(m.survivor_id); } }, `#${m.survivor_id}`)),
      ` · ${m.reason}`,
      m.reverted_at ? el("span", { class: "muted" }, ` · undone ${fmtDate(m.reverted_at)}`)
        : el("button", { class: "link", title: "Separate them again; they won't be proposed as duplicates again",
          onclick: () => unmerge([m.id], b.id) }, "Undo merge"),
    ))) : null,
    b.events.length ? el("h3", {}, "History") : null,
    b.events.length ? el("ul", { class: "events" }, ...b.events.map((e) =>
      el("li", {}, `${fmtDate(e.at)} · ${e.actor} · ${e.kind}`,
        e.data_json ? el("code", { title: e.data_json }, ` ${e.data_json.length > 200 ? e.data_json.slice(0, 200) + "…" : e.data_json}`) : null))) : null,
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

// ------------------------------------------------------------------ duplicates

const TIER_LABELS = { 3: "URL variants", 4: "Same page" };
const DUP_FIELDS = [
  ["Title", (b) => b.title || "—"],
  ["URL", (b) => b.url],
  ["Saved", (b) => fmtDate(b.saved_at, b.saved_at_source)],
  ["List", (b) => (b.deleted_at ? "Trash" : b.status === "kept" ? (b.starred ? "Kept ★" : "Kept") : "Inbox")],
  ["Sources", (b) => b.sources.map((s) => sourceLabel(s.source_ref) || s.source_type).join(", ") || "—"],
  ["Remarks", (b) => b.remarks || "—"],
  ["Tags", (b) => b.tags.join(", ") || "—"],
  ["Summary", (b) => b.description || "—"],
];
const skipped = new Set(); // groups skipped for now (until the page reloads)

async function loadDuplicates() {
  const panel = $("#panel-duplicates");
  fill(panel, el("p", { class: "muted" }, "Looking for duplicates…"));
  await api("POST", "/api/duplicates/scan");
  const { clusters } = await api("GET", "/api/duplicates");
  await loadMeta();
  const shown = clusters.filter((c) => !skipped.has(c.ids.join(",")));
  const variants = shown.filter((c) => c.min_confidence >= 0.95);
  fill(panel,
    el("div", { class: "dup-head" },
      el("h2", {}, "Duplicates"),
      el("p", { class: "muted" },
        "Bookmarks that look like the same page. Nothing is merged until you choose; a merge keeps every source, ",
        "remark and tag, and can be undone from the bookmark's details."),
      variants.length > 1
        ? el("button", { onclick: () => mergeAll(variants) }, `Merge all ${variants.length} URL-variant groups`)
        : null,
    ),
    shown.length ? null : el("p", { class: "empty" }, clusters.length ? "Nothing left to review on this page (reload to see skipped groups)." : "No duplicates found."),
    ...shown.map(clusterCard),
  );
}

function clusterCard(cluster) {
  let keep = cluster.suggested;
  const name = `keep-${cluster.ids.join("-")}`;
  const cells = (fn) => cluster.bookmarks.map((b) => fn(b));
  const differs = (fn) => new Set(cluster.bookmarks.map(fn)).size > 1;
  const why = cluster.pairs.map((p) => p.rationale).filter((r, i, all) => all.indexOf(r) === i);
  const card = el("article", { class: `dup-card tier${cluster.tier}` },
    el("header", {},
      el("span", { class: "dup-badge" }, `${TIER_LABELS[cluster.tier] || `Tier ${cluster.tier}`} · ${Math.round(cluster.confidence * 100)}%`),
      el("span", { class: "dup-why" }, why.join(" · ")),
    ),
    el("div", { class: "dup-table-wrap" },
      el("table", { class: "dup-table" },
        el("thead", {}, el("tr", {}, el("th", {}, ""), ...cells((b) => el("th", {},
          el("label", { title: "Keep this one; the others are merged into it" },
            el("input", { type: "radio", name, value: b.id, checked: b.id === keep ? "" : null, onchange: () => { keep = b.id; } }),
            ` Keep #${b.id}`, b.id === cluster.suggested ? el("span", { class: "muted" }, " (suggested)") : null),
        )))),
        el("tbody", {}, ...DUP_FIELDS.map(([label, fn]) => el("tr", { class: differs(fn) ? "diff" : "" },
          el("th", {}, label),
          ...cells((b) => el("td", {}, label === "URL"
            ? el("a", { href: safeHref(b.url), target: "_blank", rel: "noopener noreferrer" }, b.url)
            : label === "Title" ? el("a", { href: "#", onclick: (e) => { e.preventDefault(); openDetail(b.id); } }, fn(b))
            : el("div", { class: "clamp", title: fn(b) }, fn(b)))),
        ))),
      ),
    ),
    el("footer", {},
      el("button", { class: "primary", onclick: () => mergeCluster(cluster, keep, card) }, `Merge ${cluster.ids.length}`),
      el("button", { title: "Remember that these are different pages", onclick: () => dismissCluster(cluster, card) }, "Not duplicates"),
      el("button", { class: "link", onclick: () => { skipped.add(cluster.ids.join(",")); card.remove(); } }, "Skip"),
    ),
  );
  return card;
}

async function unmerge(mergeIds, reopen, remember = true) {
  try {
    for (const id of [...mergeIds].reverse()) await api("POST", `/api/merges/${id}/unmerge?remember=${remember}`);
    toast(mergeIds.length > 1 ? `${mergeIds.length} merges undone` : "Merge undone");
  } catch (err) {
    showError(err);
  }
  await refresh();
  if (state.panel === "duplicates") await loadDuplicates();
  if (reopen) await openDetail(reopen);
}

async function mergeCluster(cluster, keep, card) {
  try {
    const result = await api("POST", "/api/duplicates/merge", { keep, ids: cluster.ids, reason: cluster.pairs[0].rationale });
    card.remove();
    toast(`Merged into #${keep}`, { label: "Undo", run: () => unmerge(result.merges, null, false) });
    await loadMeta();
  } catch (err) {
    showError(err);
  }
}

async function dismissCluster(cluster, card) {
  try {
    await api("POST", "/api/duplicates/dismiss", { ids: cluster.ids });
    card.remove();
    toast("Marked as different pages");
    await loadMeta();
  } catch (err) {
    showError(err);
  }
}

async function mergeAll(groups) {
  const merged = [];
  try {
    for (const c of groups) merged.push(...(await api("POST", "/api/duplicates/merge", { keep: c.suggested, ids: c.ids, reason: c.pairs[0].rationale })).merges);
    toast(`Merged ${groups.length} groups`, { label: "Undo", run: () => unmerge(merged, null, false) });
  } catch (err) {
    showError(err);
  }
  await loadDuplicates();
}

// ------------------------------------------------------------------ categories

function categoryBadge(item) {
  if (!item.category) return null;
  const ai = item.category_assigned_by === "ai";
  return el("span", { class: `badge cat${ai ? " ai" : ""}`, title: ai ? "Set by AI (approved by you)" : "Set by you" }, item.category);
}

function categorySelect(current, onchange) {
  const select = el("select", { "aria-label": "Category", onchange: () => onchange(Number(select.value) || null) },
    el("option", { value: "" }, "— none —"),
    ...state.categories.map((c) => el("option", { value: c.id, selected: c.id === current }, c.name)));
  return select;
}

function openPanel(panel) {
  showPanel(panel);
  loadMeta();
  ({ runs: loadRuns, health: loadHealth, duplicates: loadDuplicates, categories: loadCategories, review: loadReview })[panel]().catch(showError);
}

async function loadCategories() {
  const panel = $("#panel-categories");
  const [{ categories, uncategorised }, ai, { jobs, rate }] = await Promise.all([
    api("GET", "/api/categories"), api("GET", "/api/ai/status"), api("GET", "/api/ai/jobs?limit=5"),
  ]);
  const ready = ai.parts.every((p) => p.ok);
  const busy = jobs.some((j) => ["queued", "running"].includes(j.state));
  const name = el("input", { placeholder: "New category", "aria-label": "Name" });
  const desc = el("input", { placeholder: "What belongs in it (one sentence; the AI reads this)", "aria-label": "Description", class: "wide" });
  const add = async () => {
    try {
      await api("POST", "/api/categories", { name: name.value, description: desc.value });
      await loadCategories();
      await loadMeta();
    } catch (err) {
      showError(err);
    }
  };
  fill(panel,
    el("div", { class: "dup-head" },
      el("h2", {}, "Categories"),
      el("p", { class: "muted" },
        "Each bookmark has at most one category. The AI suggests a list from a sample of your library, then sorts ",
        "bookmarks into it; every suggestion waits in AI review until you approve it. Categories you set yourself are never changed."),
      el("div", { class: "inline" },
        el("button", { disabled: !ready, title: "The local LLM reads a sample of titles and proposes 8–15 categories",
          onclick: () => startAI("taxonomy") }, categories.length ? "Suggest more categories with AI" : "Suggest categories with AI"),
        el("button", { disabled: !ready || !categories.length, title: "The local decision model picks a category for each uncategorised bookmark",
          onclick: () => startAI("categorize") }, `Categorise ${uncategorised} uncategorised with AI`),
        rate && uncategorised ? el("span", { class: "muted", title: `From job ${rate.job}: ${rate.seconds_per_item} s per bookmark${rate.tokens_per_item ? `, ${rate.tokens_per_item} input tokens each` : ""}` },
          `≈ ${duration(uncategorised * rate.seconds_per_item)} on ${rate.hardware}`) : null,
        busy ? el("span", { class: "muted" }, "An AI job is running; new ones wait in the queue. See AI review.") : null,
      ),
      ready ? null : el("details", { class: "warn" }, el("summary", {}, "The local AI isn't fully set up"),
        el("ul", {}, ...ai.parts.filter((p) => !p.ok).map((p) => el("li", {}, `${p.name}: ${p.detail}`)))),
    ),
    el("table", { class: "mini cats" },
      el("thead", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Description"), el("th", {}, "Bookmarks"), el("th", {}, ""))),
      el("tbody", {}, ...categories.map(categoryRow),
        el("tr", {}, el("td", {}, name), el("td", {}, desc), el("td", {}, ""), el("td", {}, el("button", { onclick: add }, "Add")))),
    ),
  );
}

function categoryRow(c) {
  const name = el("input", { value: c.name, "aria-label": "Name" });
  const desc = el("input", { value: c.description || "", class: "wide", "aria-label": "Description" });
  const save = async () => {
    if (name.value === c.name && desc.value === (c.description || "")) return;
    try {
      await api("PATCH", `/api/categories/${c.id}`, { name: name.value, description: desc.value });
      toast("Saved");
      await loadMeta();
    } catch (err) {
      showError(err);
    }
  };
  name.addEventListener("change", save);
  desc.addEventListener("change", save);
  const remove = async () => {
    if (c.bookmarks && !window.confirm(`Remove “${c.name}”? Its ${c.bookmarks} bookmarks become uncategorised.`)) return;
    try {
      await api("DELETE", `/api/categories/${c.id}`);
      await loadCategories();
      await loadMeta();
    } catch (err) {
      showError(err);
    }
  };
  return el("tr", {}, el("td", {}, name), el("td", {}, desc),
    el("td", {}, el("a", { href: "#", onclick: (e) => { e.preventDefault(); state.category = null; selectCategory(c.id); } }, c.bookmarks || 0)),
    el("td", {}, el("button", { class: "link danger", onclick: remove }, "Remove")));
}

async function startAI(kind) {
  try {
    const job = await api("POST", "/api/ai/jobs", { kind });
    toast(`Job ${job.id} ${job.state}: ${kind === "taxonomy" ? "suggesting categories (a few minutes)" : "categorising (a few seconds per bookmark)"}. It runs on even if you close this page.`);
    state.reviewId = null;
    openPanel("review");
  } catch (err) {
    showError(err);
  }
}

// ------------------------------------------------------------------ AI review (changesets)

const CS_KINDS = { taxonomy: "Category list", categorize: "Categorisation", agent: "Agent proposal" };
const DECISION_LABELS = { pending: "To review", approved: "Approved", rejected: "Rejected", conflict: "Conflict", applied: "Applied", reverted: "Reverted" };
let reviewTimer;

function duration(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function jobTiming(job) {
  const st = job.stats;
  if (!st.run_seconds) return null;
  const parts = [duration(st.run_seconds)];
  if (job.kind === "categorize" && st.seconds_per_item) parts.push(`${st.seconds_per_item} s per bookmark`);
  if (st.tokens_per_item) parts.push(`${st.tokens_per_item} input tokens each`);
  if (st.tokens_per_second) parts.push(`${st.tokens_per_second} tokens/s`);
  if (st.seconds_left) parts.push(`about ${duration(st.seconds_left)} left`);
  if (st.hardware) parts.push(st.hardware);
  return el("div", { class: "muted" }, parts.join(" · "));
}

const JOB_STATES = {
  queued: "Queued", running: "Running", paused: "Paused", succeeded: "Succeeded",
  succeeded_with_errors: "Succeeded with errors", failed: "Failed", cancelled: "Cancelled",
};

function jobCard(job) {
  const control = async (action) => {
    try {
      await api("POST", `/api/ai/jobs/${job.id}/${action}`);
      await loadReview();
    } catch (err) {
      showError(err);
    }
  };
  const pct = job.total ? Math.round((100 * job.done) / job.total) : null;
  const live = ["queued", "running", "paused"].includes(job.state);
  return el("div", { class: `job ${job.state}` },
    el("div", { class: "job-head" },
      el("strong", {}, `Job ${job.id} · ${job.kind === "taxonomy" ? "Suggest categories" : "Categorise"}`),
      el("span", { class: `cs-status ${job.state}` }, JOB_STATES[job.state] || job.state),
      job.total ? el("span", { class: "muted" }, `${job.done}/${job.total}`) : null,
      job.errors ? el("span", { class: "danger", title: job.error || "" }, `${job.errors} errors`) : null,
      job.started_at ? el("span", { class: "muted" }, fmtDate(job.started_at)) : null,
      el("span", { class: "spacer" }),
      job.state === "running" && job.kind === "categorize" ? el("button", { onclick: () => control("pause") }, "Pause") : null,
      job.state === "paused" ? el("button", { class: "primary", onclick: () => control("resume") }, "Resume") : null,
      live ? el("button", { class: "danger", title: "Stop; what it proposed so far still goes to review", onclick: () => control("cancel") }, "Cancel") : null,
      job.changeset_id ? el("button", { class: "link", onclick: () => { state.reviewId = job.changeset_id; loadReview().catch(showError); } }, `Proposal #${job.changeset_id}`) : null,
    ),
    pct !== null && live ? el("progress", { max: 100, value: pct }) : null,
    jobTiming(job),
    el("div", { class: "muted" }, job.state === "failed" ? job.error || "" : job.message || ""),
  );
}

async function loadReview() {
  clearTimeout(reviewTimer);
  const panel = $("#panel-review");
  const [{ changesets }, { jobs }] = await Promise.all([api("GET", "/api/changesets"), api("GET", "/api/ai/jobs?limit=5")]);
  if (state.reviewId === undefined || state.reviewId === null) {
    const open = changesets.find((c) => ["running", "pending", "partial"].includes(c.status));
    state.reviewId = open ? open.id : changesets[0]?.id ?? null;
  }
  const list = el("ul", { class: "cs-list" }, ...changesets.map((c) => el("li", {},
    el("a", { href: "#", class: c.id === state.reviewId ? "active" : "", onclick: (e) => { e.preventDefault(); state.reviewId = c.id; loadReview().catch(showError); } },
      el("strong", {}, `#${c.id} ${CS_KINDS[c.kind] || c.kind}`), el("span", { class: `cs-status ${c.status}` }, c.status),
      el("div", { class: "muted" }, `${fmtDate(c.created_at)} · ${c.author}`)),
  )));
  const detail = state.reviewId ? await changesetView(state.reviewId) : el("p", { class: "empty" }, "No proposals yet. Ask the AI for categories under Manage categories.");
  fill(panel,
    el("div", { class: "dup-head" }, el("h2", {}, "AI review"),
      el("p", { class: "muted" }, "Nothing here changes your library until you approve items and press Apply. An applied proposal can be reverted.")),
    jobs.length ? el("section", { class: "jobs" }, el("h3", {}, "AI jobs"), ...jobs.map(jobCard)) : null,
    el("div", { class: "review" }, list, detail),
  );
  if (jobs.some((j) => ["queued", "running"].includes(j.state))) reviewTimer = setTimeout(() => { if (state.panel === "review") loadReview().catch(() => {}); }, 4000);
  else loadMeta();
}

async function changesetView(id) {
  const cs = await api("GET", `/api/changesets/${id}`);
  const open = ["pending", "partial"].includes(cs.status);
  const decide = async (decision, extra) => {
    try {
      await api("POST", `/api/changesets/${id}/decide`, { decision, ...extra });
      await loadReview();
    } catch (err) {
      showError(err);
    }
  };
  const run = async (action) => {
    try {
      const r = await api("POST", `/api/changesets/${id}/${action}`);
      toast(action === "apply" ? `Applied ${r.applied}${r.conflicts ? `; ${r.conflicts} conflicts` : ""}` : `Reverted ${r.reverted}${r.skipped ? `; ${r.skipped} changed since, left alone` : ""}`);
      await refresh();
      await loadReview();
    } catch (err) {
      showError(err);
    }
  };
  const threshold = el("input", { type: "number", min: 0, max: 100, step: 5, value: 90, class: "narrow", "aria-label": "Minimum confidence (%)" });
  const hasConfidence = cs.items.some((i) => i.confidence !== null);
  const approved = cs.counts.approved || 0;
  return el("div", { class: "cs-detail" },
    el("h3", {}, `#${cs.id} ${CS_KINDS[cs.kind] || cs.kind} · ${cs.status}`),
    el("p", {}, cs.summary || "", cs.model ? el("span", { class: "muted" }, ` · ${cs.model}`) : null),
    cs.error ? el("p", { class: "warn" }, cs.error) : null,
    cs.status === "running" ? el("p", { class: "muted" }, "Running… this page refreshes by itself.") : null,
    el("p", { class: "muted" }, Object.entries(cs.counts).map(([k, v]) => `${v} ${(DECISION_LABELS[k] || k).toLowerCase()}`).join(" · ")),
    open ? el("div", { class: "inline cs-actions" },
      hasConfidence ? el("span", {}, "Approve items at ", threshold, "% or more ",
        el("button", { onclick: () => decide("approved", { min_confidence: Number(threshold.value) / 100 }) }, "Approve")) : null,
      el("button", { onclick: () => decide("approved") }, "Approve all"),
      el("button", { onclick: () => decide("rejected") }, "Reject all"),
      el("button", { class: "primary", disabled: !approved, onclick: () => run("apply") }, `Apply ${approved} approved`),
    ) : null,
    ["applied", "partial"].includes(cs.status) ? el("button", { onclick: () => run("revert") }, "Revert this proposal") : null,
    el("table", { class: "mini cs-items" },
      el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", {}, cs.kind === "taxonomy" ? "Category" : "Bookmark"),
        el("th", {}, "Change"), el("th", {}, "Confidence"), el("th", {}, "Why"), el("th", {}, ""))),
      el("tbody", {}, ...cs.items.map((i) => itemRow(cs, i, open, decide))),
    ),
  );
}

function itemRow(cs, item, open, decide) {
  const editable = open && !["applied", "reverted"].includes(item.decision);
  let subject, change;
  if (item.op === "add_category") {
    subject = el("strong", {}, item.new_value.name);
    change = el("span", {}, item.new_value.description || "");
    if (editable) {
      const name = el("input", { value: item.new_value.name, "aria-label": "Name" });
      const desc = el("input", { value: item.new_value.description || "", class: "wide", "aria-label": "Description" });
      const keep = () => decide(item.decision, { item_ids: [item.id], new_values: { [item.id]: { name: name.value, description: desc.value } } });
      name.addEventListener("change", keep);
      desc.addEventListener("change", keep);
      subject = name;
      change = desc;
    }
  } else if (item.op === "set_category") {
    subject = el("a", { href: "#", title: item.url, onclick: (e) => { e.preventDefault(); openBookmarkFromPanel(item.bookmark_id); } },
      item.title || fallbackTitle(item.url || ""));
    change = editable
      ? el("span", {}, `${item.old_name || "—"} → `, categorySelect(item.new_value, (value) => decide("approved", { item_ids: [item.id], new_values: { [item.id]: value } })))
      : `${item.old_name || "—"} → ${item.new_name || "—"}`;
  } else {
    subject = el("a", { href: "#", onclick: (e) => { e.preventDefault(); openBookmarkFromPanel(item.bookmark_id); } }, `#${item.bookmark_id} ${item.title || ""}`);
    change = `merge into #${item.target_id}`;
  }
  const conf = item.confidence === null ? "" : `${Math.round(item.confidence * 100)}%`;
  return el("tr", { class: `decision-${item.decision}` },
    el("td", {}, el("span", { class: `cs-status ${item.decision}` }, DECISION_LABELS[item.decision] || item.decision)),
    el("td", {}, subject), el("td", {}, change),
    el("td", { class: item.confidence !== null && item.confidence < 0.6 ? "low" : "" }, conf),
    el("td", { class: "muted" }, item.result && item.decision === "conflict" ? item.result : item.rationale || ""),
    el("td", { class: "actions" }, editable ? [
      el("button", { class: `link${item.decision === "approved" ? " on" : ""}`, onclick: () => decide("approved", { item_ids: [item.id] }) }, "Approve"),
      el("button", { class: "link danger", onclick: () => decide("rejected", { item_ids: [item.id] }) }, "Reject"),
    ] : null),
  );
}

// ------------------------------------------------------------------ import / export

function closeMenus() {
  for (const d of document.querySelectorAll("details.menu[open]")) d.open = false;
}

function exportList(format) {
  closeMenus();
  const params = new URLSearchParams({ format });
  if (format !== "json") {  // the list on screen, with its search and source filter
    params.set("view", state.view);
    if (state.q) params.set("q", state.q);
    if (state.source) {
      params.set("source_type", state.source.source_type);
      if (state.source.source_ref) params.set("source_ref", state.source.source_ref);
    }
  }
  location.href = `/api/export?${params}`;
}

async function upload(file, params) {
  const res = await fetch(`/api/import?${params}`, {
    method: "POST", headers: { "X-BMK-Token": TOKEN, "Content-Type": "application/octet-stream" }, body: file,
  });
  return { status: res.status, data: await res.json().catch(() => ({ detail: `HTTP ${res.status}` })) };
}

function importLine(name, data) {
  if (data.status === "imported") {
    const c = data.counts || {};
    const parts = [`${c.new || 0} new`];
    const matched = (c.matched_exact || 0) + (c.matched_alias || 0);
    if (matched) parts.push(`${matched} already here`);
    if (c.invalid || c.skipped) parts.push(`${(c.invalid || 0) + (c.skipped || 0)} skipped`);
    return `${name}: ${parts.join(", ")}`;
  }
  if (data.status === "unchanged") return `${name}: already imported`;
  return `${name}: ${data.detail || "failed"}`;
}

async function importFiles(files) {
  const lines = [];
  for (const file of files) {
    const params = new URLSearchParams({ name: file.name, mtime: file.lastModified });
    let { status, data } = await upload(file, params);
    if (status === 409 && data.columns) {
      const choice = await pickColumns(file.name, data.columns);
      if (!choice) {
        lines.push(`${file.name}: not imported`);
        continue;
      }
      for (const [k, v] of Object.entries(choice)) if (v !== "") params.set(k, v);
      ({ status, data } = await upload(file, params));
    }
    lines.push(importLine(file.name, data));
  }
  toast(lines.join(" · "), { label: "Import runs", run: () => { showPanel("runs"); loadRuns().catch(showError); } });
  await refresh();
}

async function importClipboard() {
  closeMenus();
  try {
    toast(importLine("Clipboard", await api("POST", "/api/ingest/clipboard")));
  } catch (err) {
    showError(err);
  }
  await refresh();
}

// CSV/XLSX whose URL column can't be detected: choose the columns from a preview of the first rows.
function pickColumns(fileName, cols) {
  const dialog = $("#picker");
  const option = (value, label) => el("option", { value }, label);
  const colSpec = (i) => (cols.headers[i].startsWith("column ") ? String(i + 1) : cols.headers[i]);
  const select = (none) => {
    const s = el("select", {});
    if (none) s.append(option("", "—"));
    cols.headers.forEach((h, i) => s.append(option(colSpec(i), h)));
    return s;
  };
  const url = select(false), title = select(true), date = select(true);
  const table = el("table", {});
  const paint = () => {
    const at = cols.headers.findIndex((_, i) => colSpec(i) === url.value);
    fill(table,
      el("thead", {}, el("tr", {}, ...cols.headers.map((h, i) => el("th", { class: i === at ? "pick-url" : "" }, h)))),
      el("tbody", {}, ...cols.preview.map((row) =>
        el("tr", {}, ...cols.headers.map((_, i) => el("td", { class: i === at ? "pick-url" : "", title: row[i] || "" }, row[i] || "")))))
    );
  };
  url.addEventListener("change", paint);
  paint();
  return new Promise((resolve) => {
    const done = (value) => { dialog.close(); resolve(value); };
    fill(dialog,
      el("h3", {}, `Which column has the links? — ${fileName}${cols.sheet ? ` (${cols.sheet})` : ""}`),
      el("p", { class: "muted" }, cols.error.split(";")[0] + ". The first rows are shown below."),
      el("div", { class: "cols" },
        el("label", {}, "Link (URL)", url), el("label", {}, "Title", title), el("label", {}, "Saved date", date)),
      el("div", { class: "table-wrap" }, table),
      el("div", { class: "buttons" },
        el("button", { onclick: () => done(null) }, "Cancel"),
        el("button", { class: "primary", onclick: () => done({ url_col: url.value, title_col: title.value, date_col: date.value }) }, "Import")),
    );
    dialog.addEventListener("cancel", () => resolve(null), { once: true });
    dialog.showModal();
  });
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
for (const b of document.querySelectorAll("[data-export]")) b.addEventListener("click", () => exportList(b.dataset.export));
document.querySelector('[data-import="files"]').addEventListener("click", () => { closeMenus(); $("#import-file").click(); });
document.querySelector('[data-import="clipboard"]').addEventListener("click", importClipboard);
$("#import-file").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (files.length) importFiles(files).catch(showError);
});
document.addEventListener("click", (e) => { if (!e.target.closest("details.menu")) closeMenus(); });
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
$("#bulk-category").addEventListener("change", async (e) => {
  const value = e.target.value;
  e.target.value = "";
  if (value === "") return;
  const ids = [...state.selected];
  try {
    await api("POST", "/api/bookmarks/bulk", { ids, action: "category", category_id: Number(value) });
    toast(`${ids.length} ${Number(value) ? "categorised" : "uncategorised"}`);
    state.selected.clear();
  } catch (err) {
    showError(err);
  }
  await refresh();
});
for (const a of document.querySelectorAll("[data-panel]")) {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    openPanel(a.dataset.panel);
  });
}

refresh();
// Pick up background (launchd) scans without a reload.
setInterval(() => { if (state.panel === "bookmarks" && !document.querySelector("#rows input[type=text]")) loadMeta().catch(() => {}); }, 60000);
