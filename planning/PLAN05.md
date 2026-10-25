# Bookmarks Manager for macOS — Consolidated Plan (v3, checkpoint)

*25 Oct 2026 · Consolidates PLAN03_CLAUDE, PLAN03_CHATGPT and PLAN03_GEMINI into one plan. Splits code generation into phases (§16) and records Phase 1 status (§17).*

PLAN03_CLAUDE is the base: it was the most detailed of the three. This document keeps its structure. It adds the points from PLAN03_CHATGPT and PLAN03_GEMINI that were new or better, and it settles the open decisions with defaults that can be changed later (§18).

---

## 0. Consolidation summary

### 0.1 Where the three v2 plans agree

All three agree on these, so they are settled:

- **SQLite is the only source of truth.** It runs in WAL mode with FTS5. JSON, JSONL, Markdown and CSV are exports, never a second live store.
- **CloudTabs is a live snapshot, not history.** Poll it with launchd, keep tabs after they close, and record first and last seen times.
- **Read Safari's database read-only** using the SQLite backup API. Never write to it.
- **Unknown timestamps are never set to "now".** Every timestamp records where it came from.
- **One canonical bookmark, many source records.** Every importer is idempotent.
- **Canonicalisation is conservative and driven by a rules file.** Tracking parameters are stripped, content-identifying parameters are kept, and host aliases are listed.
- **Fetching runs separately from ingestion.** It uses GET (not HEAD), handles interstitials such as lnkd.in and t.co, and uses oEmbed and other structured endpoints first.
- **Fetching is SSRF-safe.** Only http and https are allowed, private and metadata IPs are blocked, every redirect is re-checked, and size and time are capped.
- **Browser automation uses a dedicated Chrome profile that you log into by hand.** It stops on any challenge and uses no evasion. LightPanda is out of scope.
- **AI changes are reviewed changesets, not rewritten files.** IDs are validated, invalid batches are rejected and retried, and approved changes are applied in one transaction.
- **CLI and MCP sit on one service layer.** There is no raw SQL tool, and agents can only propose bulk changes.
- **Soft delete with Trash.** Deleting a bookmark never touches the source.
- **A Milestone 0 spike comes first**, and tests use fixtures rather than live sites.

### 0.2 Where they disagreed, and the decision

| Topic | Claude v2 | ChatGPT v2 | Gemini v2 | **Decision** |
|---|---|---|---|---|
| Stack | Python core, web UI | Python core, web UI | Swift 6 + SwiftUI only | **Python core + local web UI** (2 of 3). The hard parts all have Python-first libraries: trafilatura, Playwright, the MCP SDK, Bedrock, openpyxl. A SwiftUI shell is an option later (§10). |
| Bookmark ID | Integer PK | UUID or integer | UUIDv4 text | **Integer PK.** Simple, stable, and short in AI snapshots and CLI use. Never a URL hash. |
| `saved_at` | Nullable + source | Nullable + basis | `NOT NULL` | **Nullable.** `NULL` with `saved_at_source='unknown'`. |
| Trailing slash, fragments | Tier 3 only; keep fragments by default | Don't blindly strip | Strip both | **Conservative (Claude/ChatGPT).** Gemini's SPA rule is adopted: `#/route` fragments are always kept. |
| Categories | 1 category + tags | 1 category + tags (many-to-many optional) | 1 category + tags | **1 category + tags.** |
| Enrichment queue | Status columns on `bookmarks` | Separate `processing_jobs` table | Status columns | **Both.** Summary status stays on `bookmarks` for the UI. Job, retry and lease state lives in `processing_jobs` (adopted from ChatGPT). |
| User-Agent | "Normal browser UA" | "Clearly identified, don't impersonate" | Browser UA | **Honest by default**: `bmk/<ver> (personal bookmark manager)`. A per-domain override is allowed only for shorteners that refuse to redirect for non-browser clients (a documented exception, not a way around access controls). |
| Audit trail | Changesets + merges | Optional `bookmark_events` | Changesets | **Add `bookmark_events`** (ChatGPT). Every user or agent edit, delete, restore and merge is logged. |

### 0.3 Additions in this version

**From ChatGPT v2**
- More detailed ingest outcomes: `new`, `matched_exact`, `matched_alias`, `unchanged`, `invalid`, `skipped`, `suppressed_deleted`, `suppressed_resaved`.
- Title similarity on its own is never enough to merge. In tier 4 it only lowers the confidence of a proposal.
- Optional dependency groups (`[web]`, `[enrich]`, `[files]`, `[ai]`, `[browser]`, `[gmail]`) so the core install stays small.
- Confidence values are model estimates, not calibrated probabilities. They decide what to review first, never what to auto-apply.
- No vector database until keyword search and structured classification prove insufficient. Tier 5 is gated behind that.

**From Gemini v2**
- Keep `#/…` SPA hash-route fragments.
- Browser tier: at least 15–30 s between pages per site, a daily cap, and a macOS notification when a site's queue halts.
- The detail pane mock-up and the "Source" badge column.

---

## 1. Goals, non-goals and principles

**Goals**
- Capture URLs from iPhone/Mac Safari tabs and exported files with as little friction as possible.
- Keep one deduplicated, searchable, editable library on the Mac.
- Enrich each bookmark with its resolved URL, title, description and (optionally) bounded extracted text.
- Let AI propose how to organise bookmarks and which duplicates to merge. A human approves.
- Expose everything to agents through the CLI and MCP, with guardrails.

**Non-goals (v1)**
- Multi-user or multi-device writes.
- Replacing Safari's bookmarks or Reading List.
- Full-page archiving.
- Getting past any site's access controls.
- Autonomous AI changes.

**Principles**
1. **Local-first.** Everything except enrichment and AI works offline.
2. **Never lose source data.** Keep the original URL, raw title and provenance.
3. **Never write to sources.** Safari's databases, mailboxes and input files are read-only.
4. **User edits win.** Machine processes never overwrite locked fields.
5. **Every bulk change is a reviewable, revertible changeset.**
6. **Everything is idempotent.** Re-running any ingester or processor is safe.

---

## 2. Architecture

```
                ┌───────────────────── Ingesters ─────────────────────────┐
                │ cloudtabs │ rtf │ markdown │ csv/xlsx │ clipboard │ gmail* │
                └──────┬──────────────────────────────────────────────────┘
                       │  RawItem(url, title?, event_at?, source_ref, locator, …)
                       ▼
               ┌───────────────┐  validate · canonicalise · dedupe tiers 1–2 · upsert
               │ Core service  │─────────────────────────────────────┐
               │ (bmk.service) │                                     ▼
               └──────┬────────┘                          ┌───────────────────┐
                      │                                   │ SQLite (WAL)+FTS5 │
     ┌────────────────┼────────────────┐                  └───────────────────┘
     ▼                ▼                ▼                           ▲
 CLI (Typer)   HTTP API (FastAPI)  MCP server (FastMCP)            │
                      │                              ┌─────────────┴──────────────┐
                      ▼                              │ Processing worker           │
              Web UI (static JS)                     │ processing_jobs queue:      │
                                                     │ resolve → re-dedupe →       │
                                                     │ fetch (SSRF-guarded) →      │
                                                     │ extract → (browser*)        │
                                                     └─────────────────────────────┘
                                                        * Phase 3
```

- **One service layer.** The CLI, HTTP API and MCP are thin adapters, and none of them touch SQL.
- **Two pipelines.** Ingestion is fast and local, and each item always gets an outcome. Enrichment is networked and retryable. Ingestion never waits on the network.

---

## 3. Data model (SQLite)

The schema is added through numbered migrations. Each table appears in the migration for the code-generation phase that first uses it (§16), so the database never carries tables that nothing reads yet.

```sql
-- 001 (CG1) ---------------------------------------------------------------
CREATE TABLE bookmarks (
  id               INTEGER PRIMARY KEY,
  url_original     TEXT NOT NULL,          -- first URL as received (never modified)
  url_resolved     TEXT,                   -- after unshortening (CG3)
  url_canonical    TEXT NOT NULL UNIQUE,   -- primary dedupe key
  url_loose        TEXT,                   -- aggressive key for tier 3 (indexed)
  canonical_hint   TEXT,                   -- same-site rel=canonical / og:url (CG3)
  title            TEXT,
  title_source     TEXT,                   -- 'source' | 'html' | 'oembed' | 'browser' | 'user'
  description      TEXT,
  site_name        TEXT,
  domain           TEXT,                   -- registrable domain
  image_url        TEXT,
  author           TEXT,
  published_at     TEXT,
  content_text     TEXT,                   -- bounded (default 20k chars)
  content_simhash  TEXT,
  saved_at         TEXT,                   -- UTC ISO-8601, NULL if unknown
  saved_at_source  TEXT NOT NULL DEFAULT 'unknown',
                   -- 'exact' | 'user' | 'first_seen' | 'first_seen_initial' | 'file_birthtime' | 'file_mtime' | 'unknown'
  imported_at      TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  remarks          TEXT,
  category_id      INTEGER REFERENCES categories(id),
  category_assigned_by TEXT,               -- 'user' | 'ai' | 'rule'
  status           TEXT NOT NULL DEFAULT 'inbox',   -- 'inbox' | 'read' | 'archived'
  locked_fields    TEXT NOT NULL DEFAULT '[]',      -- JSON array
  fetch_status     TEXT NOT NULL DEFAULT 'pending', -- summary of processing_jobs (CG3)
  fetch_error      TEXT,
  http_status      INTEGER,
  last_fetched_at  TEXT,
  metadata_stale   INTEGER NOT NULL DEFAULT 0,
  deleted_at       TEXT,
  merged_into_id   INTEGER REFERENCES bookmarks(id)
);
CREATE TABLE url_aliases (url_canonical TEXT PRIMARY KEY, bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id),
                          kind TEXT NOT NULL);   -- 'canonical'|'original'|'short'|'redirect'|'variant'|'merged'|'canonical_hint'
CREATE TABLE sources (
  id INTEGER PRIMARY KEY, bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id),
  source_type TEXT NOT NULL, source_ref TEXT, locator TEXT,
  raw_url TEXT NOT NULL, raw_title TEXT, event_at TEXT,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, import_run_id INTEGER,
  UNIQUE (source_type, source_ref, locator, raw_url)
);
CREATE TABLE import_runs (...);   -- source_type, params, started/finished, counts per outcome, error
CREATE TABLE import_items (...);  -- run, locator, raw_url, raw_title, raw_timestamp, outcome, bookmark_id, error
CREATE TABLE categories (...); CREATE TABLE tags (...); CREATE TABLE bookmark_tags (... assigned_by ...);
CREATE TABLE bookmark_events (id, bookmark_id, at, actor, kind, data_json);  -- 'edit'|'delete'|'restore'|'merge'|…
CREATE VIRTUAL TABLE bookmarks_fts USING fts5(title, description, content_text, remarks, url_canonical,
                                              content='bookmarks', content_rowid='id');  -- + sync triggers

-- 002 (CG3) processing_jobs(id, bookmark_id, kind, state, attempts, next_run_at, lease_until, last_error, …)
-- 003 (CG5) merges(… snapshot_json, reverted_at), not_duplicates(a_id, b_id, decided_at)
-- 004 (CG6) changesets(… provider, model, snapshot_id, prompt_version …), changeset_items(… base_updated_at, decision …)
```

The full DDL is in `src/bmk/migrations/001_init.sql`. The design notes from PLAN03_CLAUDE §4 still hold:
- Default views exclude rows that are deleted or merged.
- `locked_fields` is set by user edits.
- AI only changes assignments that are empty or `assigned_by='ai'`.
- Timestamps are stored in UTC and displayed in Asia/Singapore time.
- Lists are paginated, and every multi-row write runs in a transaction.

**Source uniqueness by source type** (this addresses ChatGPT's concern about repeated captures):

| Source | `source_ref` | `locator` |
|---|---|---|
| CloudTabs | device name | tab UUID |
| Markdown / RTF | file path | line or ordinal |
| CSV / XLSX | file path | `Sheet!R42` |
| Gmail | message-id | URL ordinal |
| Manual / MCP | `manual` / `mcp` | timestamp of the add |

A genuinely repeated capture always gets a new row.

---

## 4. Ingestion

Every ingester implements `Ingester.read() -> Iterable[RawItem]`. The core then does the following for each item:

1. **Validate.** The URL must parse and must be `http` or `https`. Other schemes (`about:`, `file:`, `javascript:`, `favorites://`) are recorded as `skipped`, with a reason. Unparseable URLs are recorded as `invalid`.
2. **Canonicalise** (§6.3).
3. **Tier 1 lookup.** Check `url_canonical`, then `url_aliases`.
4. **Upsert** the bookmark and its `sources` row, and apply the timestamp policy (§4.1). Apply tombstone rules (§7.7).
5. **Record** an `import_items` outcome.
6. **Queue enrichment** (from CG3).
7. **Return a run summary** with counts for each outcome.

One bad item never fails the batch.

### 4.1 Timestamp policy

`saved_at` is set by precedence:

`exact` (source event) > `user` > `first_seen` > `first_seen_initial` > `file_birthtime` > `file_mtime` > `unknown` (NULL, never "now")

When a duplicate arrives:
- If it has **better precision**, it replaces `saved_at`.
- If it has **equal precision and an earlier time**, it moves `saved_at` earlier.
- If `saved_at` is locked, nothing changes.

CloudTabs: the first time a *device* is polled, its tabs get `first_seen_initial`, because they may have been open for a long time already. Tabs that appear on later polls get `first_seen`.

The UI shows `~` for approximate dates and `—` for unknown ones. Unknown dates sort last, and there is a *Date unknown* smart list.

### 4.2 Priority 1: Safari iCloud Tabs (`CloudTabs.db`)

**Paths:** `~/Library/Containers/com.apple.Safari/Data/Library/Safari/CloudTabs.db`, then `~/Library/Safari/CloudTabs.db`. `BMK_CLOUDTABS_PATH` overrides both.

**Expected schema** (to be verified in the spike):
- `cloud_tabs`: tab_uuid, device_uuid, title, url, position, …
- `cloud_tab_devices`: device_uuid, device_name, …

The ingester introspects the schema with `PRAGMA table_info`. It depends only on url, title and device_uuid → device_name, plus tab_uuid when present. Timestamp columns are ignored until the spike confirms what they mean.

**Read access:**
1. Open the file read-only with `mode=ro`.
2. Snapshot it to memory with `Connection.backup()`.
3. If step 1 fails, copy the `.db`, `-wal` and `-shm` files together to a temp directory and open that copy.

The ingester never writes to Safari's files.

**Full Disk Access:**
- During development it is granted to the terminal.
- For launchd it is granted to the exact binary launchd runs. A small signed helper is an option later.
- "Operation not permitted" / "authorization denied" is detected, and the exact System Settings fix is shown.

**Polling:** a launchd agent polls every 30 minutes. Tabs that close are kept. `--devices` and `--exclude-devices` filter which devices are captured. Deleted bookmarks stay deleted when their tab is seen again (§7.7).

### 4.3–4.6 RTF, Markdown, CSV/XLSX, Email

Unchanged from PLAN03_CLAUDE §5.3–5.6:

- **RTF:** `textutil` → HTML parser, plus plain-text URLs. The clipboard ingester is built on the same parser.
- **Markdown:** markdown-it tokens, autolinks and bare URLs. URLs inside code blocks are skipped, folders are tracked by content hash, and the iOS Shortcut `inbox.md` format is supported with exact timestamps.
- **CSV/XLSX:** columns are detected automatically, with a picker in the UI when detection is ambiguous. XLSX hyperlink targets are read, and provenance is recorded as sheet!row.
- **Gmail (Phase 3):** `gmail.readonly` scope, reads only the `Bookmarks` label, preview mode, and incremental sync with `historyId`. Tokens are kept in the Keychain.
- **Apple Mail:** AppleScript/JXA or `.eml` import.

---

## 5. (Numbering intentionally skips §5 so §6 onward matches PLAN03_CLAUDE)

---

## 6. Processing (enrichment) pipeline

`processing_jobs` holds the job state: pending, leased, retry_wait, done and dead. Each job has attempt counts and a backoff. `bookmarks.fetch_status` mirrors a summary for the UI.

```
pending → resolve (unshorten/unwrap) → re-canonicalise → dedupe tier 2 → fetch → extract → fetched | partial
                                                                 ↘ needs_browser   ↘ failed → dead   ↘ skipped
```

- **6.1 Unshortening.** GET with a 10-hop cap and loop detection. Handles meta refresh and simple JS `location=` redirects. Has per-shortener extractors for `lnkd.in` and `t.co`. Unwraps known redirect wrappers without a network call: LinkedIn authwall and redir, `l.facebook.com`, `google.com/url`, `out.reddit.com`. Each resolution is cached as a `url_aliases` row. When resolution fails, the original URL is kept.
- **6.2 SSRF guard.** Only http and https. DNS results that point to loopback, RFC 1918, link-local, CGNAT, metadata or IPv6-equivalent addresses are blocked, and this is re-checked on every hop and at connect time. Limits: 5 s connect, 20 s total, 5 MB response, HTML only. No cookies or credentials are sent.
- **6.3 Canonicalisation.** Rules live in `src/bmk/rules/canonicalize.yaml`. Implemented in CG1:
  - Lowercase the scheme and host, convert IDN to punycode, and drop default ports.
  - Normalise percent-encoding, collapse `//` in paths, and use `/` for an empty path.
  - Strip known tracking parameters (global and per-domain) and keep unknown parameters. Sort what remains, and remove an empty `?`.
  - Fragments are kept by default. `#:~:text=` is dropped, `#/route` is always kept, and fragments are dropped on domains listed as non-meaningful.
  - Host aliases: twitter → x.com, `m.`/mobile hosts, `youtu.be/ID` → `youtube.com/watch?v=ID`, and `en.m.wikipedia` → `en.wikipedia`.
  - `www.` is stripped except on listed exception domains.
  - `url_loose` is the tier-3 key: no scheme, no `www`, no trailing slash, and only query-significant parameters kept.
- **6.4 Metadata.** Pages are fetched only when needed (missing or generic title, or text wanted). Order: oEmbed and structured endpoints, then trafilatura, then OG/Twitter meta, then `<title>`. Title fallback: locked user title, then oEmbed/OG, then HTML (unless it's generic), then the source title, then domain + path. `canonical_hint` is accepted only from the same site, and simhash is computed. Politeness: 1–2 concurrent requests per domain and about 8 globally, with 429/Retry-After honoured. Blocks (403/999, challenge pages, login walls) go to `needs_browser` and are not retried blindly.

---

## 7. Deduplication

Unchanged from PLAN03_CLAUDE §7, with one ChatGPT refinement noted under tier 4.

| Tier | Signal | Action | Phase |
|---|---|---|---|
| 1 | Same `url_canonical`, or a hit in `url_aliases` | Auto-attach at ingest | **CG1** |
| 2 | Same URL after resolve/unwrap | Auto-merge | CG3 |
| 3 | Same `url_loose` (http/https, `www`, trailing slash, AMP, mobile, non-significant query) | Propose | CG5 |
| 4 | Same-site `canonical_hint`, simhash ≤ 3, or same domain + normalised title (**title alone gives low confidence only**) | Propose | CG5 |
| 5 | Embedding similarity | Propose as related or duplicate | CG6 (gated) |

**Other parts of the design:**
- **Guards:** query-significant parameters per domain, generic titles ignored, and `not_duplicates` remembered.
- **Survivor selection:** most locked fields, then earliest best-precision `saved_at`, then lowest ID.
- **Field-level merge rules:**
  - Remarks are concatenated.
  - Tags are combined.
  - The user's category wins.
  - The most advanced status wins.
  - Sources are re-pointed to the survivor.
- **Un-merge:** reversible from `snapshot_json`.
- **Review:** Duplicates review UI.
- **§7.7 Tombstones.** Delete is soft and keeps the canonical URL and aliases.
  - A deleted bookmark seen again is recorded as `suppressed_deleted` and stays deleted.
  - A *new* source item with an `event_at` later than `deleted_at` is recorded as `suppressed_resaved` and shown in a "Deleted but saved again" list. Setting `BMK_RESURRECT_ON_NEW_SAVE=false` is the default.
  - Emptying the Trash keeps URL-only tombstones.
- **§7.8 acceptance tests:** unchanged.

---

## 8. Browser-assisted extraction (Phase 3)

Unchanged from PLAN03_CLAUDE §8, plus the pacing from Gemini.

**Sequencing and access**
- Measure how many items end up in `needs_browser` first.
- Tier A: real Chrome with a dedicated profile over CDP, logged in by hand.
- Tier B: headless Playwright, for public pages only.

**Rules**
- No evasion.
- Visit only saved URLs, and only read from them.
- **At least 15–30 s between pages on a site**, with a daily cap per site.
- On a challenge, stop that site's queue and send a macOS notification.

**Safety**
- The browser profile counts as credentials.
- Browser enrichment is off by default.
- LinkedIn's terms are a known risk, so leaving LinkedIn off is a sensible default.

---

## 9. Storage, backup and sync

| Concern | Decision |
|---|---|
| Live database | `~/Library/Application Support/bmk/bookmarks.db` (0700). `BMK_DB_PATH` overrides it. Never kept in iCloud Drive. |
| Backup / restore | `bmk export --format json` and `bmk import-backup` give an exact round trip. Built in **CG1**, so the format is tested from day one. |
| Other exports | JSONL (agents), CSV, Markdown and Netscape HTML, in CG4 |
| Phase 3 | Litestream → S3, or a nightly `.backup` to iCloud Drive, plus a one-way Google Sheets export |
| Two-way sync | Deferred until requirements are defined. DynamoDB with last-write-wins and tombstones is the likely fit if it's ever needed. |

---

## 10. Viewer

**v1:** FastAPI on `127.0.0.1`, serving a static vanilla JS UI that uses Tabulator, with no build step. Write calls need a token from `~/Library/Application Support/bmk/token`. Layout, columns and features follow PLAN03_CLAUDE §10:

- **Smart lists:** All, Inbox, Unprocessed, Failed/Blocked, Duplicates, Date unknown, Deleted but saved again, Categories, Trash.
- **Editing:** inline, and an edit locks the field.
- **Bulk actions**, a detail pane and an import-run panel.
- **Source badge column** (from Gemini).

**Later:** a SwiftUI shell around `WKWebView` (menu bar icon, capture hotkey), or a native client on the same API.

---

## 11. Agentic interface (Phase 2)

Unchanged from PLAN03_CLAUDE §11. The CLI and MCP share the same set of operations.

**Guardrails**
- No raw SQL.
- Write tools act on single items. Anything bulk goes through a changeset.
- Deletes are soft only.
- `ingest` only reads from allowlisted paths.
- `MCP_ALLOW_WRITES=false` gives a read-only server.

**AI categorisation**
1. Take a JSONL snapshot.
2. Propose a taxonomy.
3. You edit the taxonomy.
4. Classify in batches against the fixed taxonomy.
5. Validate. Invalid batches are rejected and retried.
6. Build a changeset. It records `base_updated_at`, model, provider and prompt version.
7. Review.
8. Apply in one transaction. Items that changed since the snapshot come back as conflicts.
9. Revert if needed.

**Providers:** `AI_PROVIDER=bedrock` is the default, since the Bedrock setup already exists. Anthropic and `openai_compat` (local MLX) are the alternatives. External AI providers are opt-in (`AI_EXTERNAL_ALLOWED`).

---

## 12. Privacy, security and reliability

Unchanged from PLAN03_CLAUDE §12.
- Secrets go in the Keychain.
- Logs never contain message bodies, cookies or tokens.
- The server listens only on localhost and needs a token for writes.
- `bmk doctor` checks Full Disk Access, the CloudTabs path and schema, database integrity and the launchd agent.

---

## 13. Testing and quality gates

Unchanged from PLAN03_CLAUDE §13.
- Unit, integration and regression fixtures.
- No live sites in automated tests (`respx` mocks HTTP).
- `bmk selftest --live` runs manually.

**Definition of done:**
1. Acceptance criteria met.
2. Tests passing.
3. Errors are visible.
4. Data is intact.
5. Exercised for real.
6. Docs updated.

---

## 14. Tech stack and layout

| Area | Choice |
|---|---|
| Language | Python 3.13 (`~/.venv`, `uv pip install -e ".[dev]"`) |
| Core dependencies | `typer`, `python-dotenv`, `pyyaml`, `tldextract` (bundled suffix list, offline) |
| Extras | `web` = fastapi, uvicorn · `enrich` = httpx, trafilatura, selectolax, tenacity · `files` = openpyxl, markdown-it-py · `macos` = pyobjc-framework-Cocoa · `ai` = boto3, anthropic, mcp · `browser` = playwright · `gmail` = google-api-python-client, google-auth-oauthlib · `dev` = pytest, respx |
| Database | stdlib `sqlite3` (WAL, FTS5, backup API) |

```
agentic-bookmarks/
├── pyproject.toml · .env.example · .gitignore · README.md
├── scripts/spike_cloudtabs.py
├── docs/cloudtabs-schema.md          # generated by the spike
├── src/bmk/
│   ├── config.py · db.py · models.py · timeutil.py · urls.py · service.py · cli.py
│   ├── migrations/001_init.sql …
│   ├── rules/canonicalize.yaml
│   ├── ingest/{base,cloudtabs,rtf,markdown,tabular,clipboard,gmail}.py
│   ├── process/{worker,resolve,fetch_guard,extract,oembed,browser}.py      (CG3)
│   ├── dedupe/{scan,merge,unmerge}.py                                      (CG5)
│   ├── ai/… · api.py · mcp_server.py · doctor.py
├── web/{index.html,app.js,styles.css}                                      (CG2)
├── launchd/com.bmk.cloudtabs.plist                                         (CG2)
└── tests/{unit,integration,fixtures}/
```

---

## 15. Product roadmap (unchanged milestones)

- **M0:** spike.
- **1a:** vertical slice CloudTabs → SQLite → UI.
- **1b:** processing.
- **1c:** file ingesters and dedupe review.
- **2:** agentic.
- **3:** hard parts.

The **Phase 1a acceptance slice** (8 scripted steps) is unchanged from PLAN03_CLAUDE §15.2 and must pass at the end of CG2.

---

## 16. Code generation phases

Each code-generation (CG) phase ends with all tests passing and a checkpoint update to §17.

| CG | Scope | Delivers | Exit criteria |
|---|---|---|---|
| **CG1** | **M0 spike + core foundation + CloudTabs + CLI** | Project scaffold and extras · config · migrations 001 · URL canonicalisation and rules file · timestamp policy · service layer (ingest with tier-1 dedupe, aliases, tombstones, locked fields, events, list/search/show/edit/delete/restore, JSON export and restore) · CloudTabs ingester (introspection, backup API, copy fallback, FDA detection, device filters) · spike script · CLI · tests | `pytest` is green. On a fixture CloudTabs.db, `bmk ingest cloudtabs` is idempotent, a vanished tab is kept, a deleted bookmark is suppressed, an edited title persists, and the export/restore round trip is identical. Once Full Disk Access is granted, the spike prints the real tabs. |
| CG2 | Viewer + operations | FastAPI API (token-protected writes) · web UI (table, smart lists, inline edit, delete/undo/Trash, detail pane, import runs) · launchd poller and install command · `bmk doctor` | The Phase 1a acceptance slice (§15) passes end to end on the real Mac |
| CG3 | Enrichment (1b) | migration 002 · worker · SSRF guard · unshortening and unwrapping · oEmbed · trafilatura · tier 2 · retry and refetch in the UI | §15.3 criteria (lnkd.in resolves, private IP blocked, no retry storms) |
| CG4 | File ingesters (1c-i) | RTF (textutil) · clipboard · Markdown and iOS Shortcut · CSV/XLSX with column picker · JSONL/CSV/MD/Netscape export | The same URL from RTF, Markdown and CloudTabs gives one bookmark with three sources |
| CG5 | Dedupe review (1c-ii) | migration 003 · tier 3–4 scan · Duplicates UI · merge/unmerge · `not_duplicates` · recanonicalise | All §7.8 acceptance tests pass |
| CG6 | Agentic (2) | migration 004 · changesets · AI taxonomy and classification (Bedrock) · review UI · MCP server · full CLI · (tier 5 gated) | Claude, through MCP, can search and propose. Nothing changes until approved, and conflicts are detected. |
| CG7 | Hard parts (3) | Gmail · browser tier (after measuring) · backups · Sheets export · optional SwiftUI shell | Per feature |

---

## 17. Checkpoint: status

| CG | Status | Notes |
|---|---|---|
| CG1 | **Code complete; the real-Mac spike passed.** | **Built:** project scaffold with optional extras · `config` · migration 001 (with FTS5 triggers) · `urls` (canonical key + loose key, rules in YAML) · `timeutil` (precedence, `~`/`—` display) · `service.Library` (ingest with tier 1 dedupe and aliases, per-item savepoints, outcomes, first_seen_initial per new device, tombstones and re-save detection, locked-field edits and events, URL edit keeps the old URL as an alias, list/search/show, JSON backup and restore) · CloudTabs ingester (path probe, Full Disk Access detection, backup-API snapshot plus copy fallback, schema introspection, device filters) · `scripts/spike_cloudtabs.py` · `bmk` CLI (`init, ingest cloudtabs, add, list, search, show, edit, rm, restore, runs, export, import-backup`).<br>**Spike run on 25 Oct 2026 (macOS 26.5.2, Safari 26.5.2):** read 485 tabs across 3 devices from the sandboxed path, using the backup-API snapshot. The schema matches the expected columns, plus two not in the plan: `cloud_tabs.last_viewed_time` (REAL, Mac absolute time) and `cloud_tab_devices.device_type_identifier`. `system_fields` and other columns are NOT NULL, which broke the first `--make-fixture` run; that's fixed. Findings are in `docs/cloudtabs-schema.md`. **`last_viewed_time` is now used.** It is set on all 485 tabs, and the values range from Dec 2025 to the day of the spike, so it is a per-tab time. A tab can't have been viewed before it was opened, so the ingester passes it as `RawItem.observed_by`. When it is earlier than the poll time, it becomes the `first_seen` / `first_seen_initial` saved_at; it is still labelled approximate. Values that are 0, NULL or in the future fall back to the poll time. `cloud_tab_devices.last_modified` is rounded to local midnight and is not used. **Still to check:** a tab opened at a known time should show a matching `last_viewed_time`.<br>**Implementation notes:** (1) A read-only SQLite reader still writes read marks into Safari's `-shm` lock file. That is normal SQLite behaviour. The `.db` and `-wal` files are verified unchanged. (2) `first_seen` and `first_seen_initial` share a precedence rank, so the earliest observation wins. (3) A user URL edit stores the new URL in `url_resolved` and locks `url`, so CG3 enrichment must check that lock before resolving. |
| CG2–CG7 | Not started | |

Environment checked on 25 Oct 2026: macOS 26.5.2, Safari 26.5.2, Python 3.13.9 (`~/.venv`), uv, SQLite 3.51.0.

---

## 18. Decisions taken as defaults (override any time)

1. **Stack:** Python core and a local web UI. A SwiftUI shell may come later.
2. **Categories:** one category plus tags.
3. **CloudTabs scope:** all devices by default. `--exclude-devices` can be used, for example to exclude this Mac.
4. **Poll interval:** 30 minutes.
5. **Re-saved after delete:** stays deleted and appears in a list.
6. **Content storage:** bounded text, 20k characters, configurable.
7. **AI provider:** Bedrock. External providers are opt-in.
8. **iOS Shortcut capture:** in CG4.
9. **Bookmark ID:** integer.
10. **User-Agent:** honest by default.
