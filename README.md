# agentic-bookmarks

Bookmark Manager with AI Agent support — a local-first bookmarks library for macOS.

`bmk` captures URLs (starting with Safari iCloud Tabs), deduplicates them, keeps full provenance, and
stores everything in a single SQLite database. A web UI, URL enrichment, more importers and an
AI/MCP interface follow in later phases — see [planning/PLAN05.md](planning/PLAN05.md).

## Status

Code-generation phase **CG1** (core + CloudTabs + CLI). See PLAN05 §16–17.

## Install

```bash
source ~/.venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest
```

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
bmk list --view trash | date_unknown | all
bmk search "bedrock agents"
bmk show 12
bmk edit 12 --title "Better title" --saved-at 2026-10-01T09:00+08:00   # edited fields are locked
bmk rm 12 && bmk restore 12               # soft delete / Trash
bmk runs                                  # import history; `bmk runs <id>` for per-item outcomes
bmk export -o backup.json                 # full backup
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
