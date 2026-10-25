# agentic-bookmarks

Bookmark Manager with AI Agent support — a local-first bookmarks library for macOS.

`bmk` captures URLs (starting with Safari iCloud Tabs), deduplicates them, keeps full provenance, and
stores everything in a single SQLite database, with a web UI, page fetching, file importers, duplicate
review, local-AI categories and an MCP server — see [planning/PLAN05.md](planning/PLAN05.md).

## Status

Code-generation phase **CG6** (AI categories, MCP server) is done. Earlier: CG1 (core, CloudTabs, CLI),
CG2 (web UI, background scan, `bmk doctor`), CG3 (page fetching), CG4 (file importers, exports),
CG5 (duplicate review).
See PLAN05 §16–17.

## Install

```bash
source ~/.venv/bin/activate
uv pip install -e ".[dev,files,macos]"   # dev includes web and enrich; files = Markdown/XLSX, macos = clipboard
# optional: ".[ai,mcp]" and scripts/install-ai.sh — see Categories and AI review
python -m pytest
```

## Web UI

```bash
bmk serve --open                # http://127.0.0.1:8765/ (BMK_PORT to change)
```

**Triage:** everything new lands in the **Inbox**. Each row has **✓ Keep** (moves it to **Kept**) and
**🗑 Trash**; both have Undo, and Trash → Restore works any time. **☆ Star** keeps a bookmark and adds it
to **Starred**, a short list of the most interesting ones. Keys: ↓ ↑ move, S star, K keep, D trash,
O open, Enter details. A bookmark is stored in the local library as soon as it is scanned, so closing
the tab on your phone never loses it.

Also: lists (Inbox, Kept, All, Date unknown, Deleted but saved again, Trash), per-device filters, search,
sorting, inline edit (double-click a title or remark; edits are locked 🔒), bulk actions,
delete with Undo, a detail pane (URLs, aliases, sources, history, set the saved date), import runs with
per-item outcomes, and a Health panel. The server listens on 127.0.0.1 only and every write needs the
token in `~/Library/Application Support/bmk/token`, which the served page carries.

## Importing files and exporting

```bash
bmk ingest file notes/ tabs.rtf list.csv  # any mix; folders are read recursively
bmk ingest rtf tabs.rtf                   # RTF via macOS textutil: links + their text, plus bare URLs
bmk ingest markdown ~/iCloud/Bookmarks/   # [title](url), <autolinks>, bare URLs; code is skipped (--include-code)
bmk ingest csv list.csv --url-col Link --title-col Name --date-col Added
bmk ingest xlsx book.xlsx --sheet Reading # hyperlink targets are read, not just the cell text
bmk ingest clipboard                      # links copied from Safari, Notes or a web page
bmk export --format html -o bookmarks.html   # Netscape file any browser imports; also csv, markdown, jsonl
bmk export --format markdown --view starred
```

- A file whose content was imported before is skipped (by SHA-256), even under another name; `--force` reads it again.
- Links get the file's creation time as an approximate (`~`) save time, unless the file says better:
  an iOS Shortcut line `- 2026-10-25T13:30+08:00 [Title](URL)` or a CSV/XLSX date column is exact.
- CSV/XLSX: the URL column is a header like url/link/website, else the column that is >80% URLs. When that
  is ambiguous the CLI shows the columns and a preview; the web UI shows a column picker. Other columns
  become remarks on new bookmarks, and rows without a valid URL are recorded as invalid.
- Provenance: RTF/HTML `#n` (link order), Markdown `line n`, CSV `row n`, XLSX `Sheet!Rn`.
- A file that can't be read is reported (and recorded as a failed import run); the other files still import.
- Web UI: **Import ▾** (Files…, From clipboard) and **Export ▾**, which exports the list on screen with its
  search and device filter.

## Duplicates

The same page saved twice under slightly different addresses is merged automatically only when that is
certain (tracking parameters, short links that resolve to it). Everything less certain is *proposed* in
**Duplicates** (sidebar), most confident first:

- **URL variants (95%)**: the same address apart from http/https, `www`, a trailing slash, AMP or index pages.
  Content parameters such as YouTube's `v=` are never ignored.
- **Same page (50–90%)**: the page names the other as its canonical address, or its text is nearly identical
  with matching titles, or (low confidence) the same site and title.

Each group shows the bookmarks side by side with differences highlighted. **Merge** keeps the one you pick
(suggested: the one you've edited, else the earliest dated) and combines sources, remarks, tags, the star and
the best date. **Not duplicates** is remembered. A merge can be undone from the toast or from the
bookmark's details (**Merges → Undo merge**); undoing from the details also remembers the pair as different.

```bash
bmk dedupe show                           # groups to review (★ = suggested one to keep)
bmk dedupe merge 12 40                    # keep 12, merge 40 into it
bmk dedupe dismiss 12 40                  # different pages; never proposed again
bmk dedupe apply --min-confidence 0.95    # merge every URL-variant group (lists them and asks first)
bmk dedupe history && bmk dedupe unmerge 7
bmk dedupe recanonicalise --dry-run       # after editing src/bmk/rules/canonicalize.yaml
```

Thresholds and ignored titles: `src/bmk/rules/dedupe.yaml`.

## Categories and AI review

Each bookmark has at most one category. Set it yourself (details pane, the bulk bar, `bmk categories set`), or
let the local AI propose. Every AI or agent proposal is a *changeset* in **AI review**: nothing changes until
you approve items and press **Apply**, and an applied changeset can be **reverted**. Categories you set
yourself are never touched by the AI; an item whose bookmark changed meanwhile becomes a conflict.

1. **Suggest categories** (Manage categories, or `bmk ai taxonomy`): a local LLM reads a sample of 400
   titles and proposes 8–15 categories with one-sentence descriptions. Edit or reject any before applying.
2. **Categorise** (`bmk ai categorize`): a local decision model picks one of the categories (or "other",
   which proposes nothing) for each uncategorised bookmark, with a confidence. Review the least confident
   first; approve everything above a threshold in one click.

Everything runs on the Mac; models live in the Hugging Face cache (`~/.cache/huggingface/hub`).

Each run is an **AI job** in its own process, separate from the web server: closing the page or stopping
`bmk serve` doesn't stop it. One job runs at a time; others wait. **AI review** shows each job's state —
queued, running, paused, succeeded, succeeded with errors (some bookmarks couldn't be classified),
failed, cancelled — with progress and Pause / Resume / Cancel. A paused classification resumes where it
stopped; a cancelled one still hands over what it proposed. A job whose process dies is marked failed.

Your category list is also saved to `categories.json` next to the library (rewritten on every change; never
in the repo). `bmk categories import [file]` loads it into another library.

| Piece | Default | Setting |
|---|---|---|
| LLM (category list) | `mlx-community/Qwen3.8-27B-4bit` via mlx-lm (~16 GB; 24 GB Macs work) | `BMK_LLM_MODEL` |
| Decision model | JevK5-9B Q5_K_M GGUF via llama.cpp's `llama-server` (~6.5 GB, ~1 s per bookmark) | `BMK_DECIDER_*` |

```bash
uv pip install -e ".[ai,mcp]" && scripts/install-ai.sh   # mlx-lm, MCP SDK; jevk5 client (+ brew install llama.cpp)
bmk ai download                     # fetch the models (asks first)
bmk ai status
bmk ai taxonomy                     # → changeset; bmk changeset show / approve --all / apply
bmk ai categorize                   # → changeset; approve --min-confidence 0.9, apply (Ctrl-C pauses)
bmk ai categorize --background      # as a detached job
bmk ai jobs                         # states and progress; bmk ai pause|resume|cancel 4
bmk changeset list | revert 3
bmk categories add "Recipes" -d "Cooking and food."
```

## MCP server

`bmk mcp` serves the library to Claude (or any MCP client) over stdio. Register it with Claude Code:

```bash
claude mcp add bmk -- ~/.local/share/bmk/venv/bin/bmk mcp
```

- **Read**: `search_bookmarks`, `list_bookmarks`, `get_bookmark`, `list_categories`, `find_duplicates`,
  `list_changesets`, `get_changeset`.
- **Propose** (a changeset for you to review; there is no apply tool): `propose_categories`,
  `propose_new_categories`, `propose_merge`. So you can also ask Claude to suggest the category list instead
  of the local LLM; the titles it reads are then sent to Anthropic.
- **Single-item writes** (`add_bookmark`, `update_bookmark`, `triage_bookmark` — keep, star, Trash) exist only
  with `BMK_MCP_ALLOW_WRITES=true`. Nothing is hard-deleted.

## Page fetching (enrichment)

Each new bookmark's page is fetched once, after every scan (up to `BMK_PROCESS_BUDGET` seconds) and by
`bmk serve` in the background. Short links (lnkd.in, t.co, …) and redirect wrappers are expanded; if the
destination is already in the library, the two bookmarks merge. The page's summary, image, author, date and
article text are stored (the text is searchable) and shown as a preview card in the detail pane, with an
optional **Live preview** of the real page for sites that allow it.

- The title Safari showed is kept; a fetched title only fills in a missing or meaningless one.
- Sites that answer with a bot check or a login wall go to **Fetch failed / blocked** after one try.
- Google searches are not fetched. Nothing is fetched from private addresses (your Mac, your network).

```bash
bmk process                     # fetch everything due now (progress per bookmark)
bmk process 12 34               # fetch these again
bmk process --retry-failed      # try failed and blocked ones again
```

## Background scan (launchd)

```bash
bmk launchd install             # scans iCloud Tabs every 30 min (BMK_POLL_INTERVAL) and at login
bmk launchd status
bmk doctor                      # database, Full Disk Access, last scan, agent
```

**Run the background scan from committed code:** `scripts/install-stable.sh` installs HEAD into a separate
venv (`~/.local/share/bmk/venv`) and points the agent at it, so half-finished work in this checkout never
touches the real library. Re-run it after each commit you want live; for daily use run that copy's UI:
`~/.local/share/bmk/venv/bin/bmk serve --open`.

launchd runs the Python interpreter directly, so **Full Disk Access must be granted to that
interpreter**, not just your terminal; `bmk launchd install` and `bmk doctor` print its path. That grant
covers anything run with this interpreter. Log: `~/Library/Logs/bmk/cloudtabs.log`, also shown in the
web UI under Health → Show log. Remove with
`bmk launchd uninstall`.

## Safari access (Full Disk Access)

Safari keeps `CloudTabs.db` in a protected container. Grant **Full Disk Access** to your terminal app
(System Settings → Privacy & Security → Full Disk Access), then restart the terminal. bmk only ever
reads a snapshot of Safari's database; it never writes to it.

Milestone 0 spike (prints `device | title | url`, records the schema, makes a sanitised fixture):

```bash
python scripts/spike_cloudtabs.py --limit 20 \
  --report docs/cloudtabs-schema.md \
  --make-fixture tests/fixtures/cloudtabs_real_sanitised.db
```

## Usage

```bash
bmk ingest cloudtabs                      # import open iCloud tabs (idempotent; safe to re-run)
bmk ingest cloudtabs --exclude-device "My MacBook" --dry-run
bmk add https://example.com --title "Example" --remarks "why I saved it"
bmk list                                  # newest first; ~ = approximate date, — = unknown
bmk list --view inbox | kept | trash | date_unknown | all
bmk search "bedrock agents"
bmk show 12
bmk edit 12 --title "Better title" --saved-at 2026-10-01T09:00+08:00   # edited fields are locked
bmk keep 12                               # Inbox → Kept
bmk star 12 [--off]                       # keep + Starred (or remove the star)
bmk list --view unprocessed | failed      # not fetched yet / fetch failed or blocked
bmk rm 12 && bmk restore 12               # soft delete / Trash
bmk runs                                  # import history; `bmk runs <id>` for per-item outcomes
bmk export -o backup.json                 # full backup (restorable)
BMK_DB_PATH=/tmp/fresh.db bmk import-backup backup.json
```

## How it behaves

- **Dedupe (tier 1):** URLs are canonicalised conservatively (tracking params stripped, host aliases
  like twitter.com → x.com, unknown params kept). The same page from several sources becomes one
  bookmark with several `sources` rows. Rules: `src/bmk/rules/canonicalize.yaml`.
- **CloudTabs is a live snapshot:** tabs that are later closed stay in the library. The first poll of a
  device marks its tabs `~` (already open for an unknown time); tabs seen later get their first-seen time.
- **Deleted stays deleted:** re-seeing a deleted URL records `suppressed_deleted` instead of bringing it back.
- **Your edits win:** editing a field locks it against later automatic updates.
- **Timestamps are never invented:** unknown save times stay unknown (`—`), separate from `imported_at`.
