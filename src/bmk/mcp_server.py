"""MCP server (PLAN05 §11): Claude (or any MCP client) can search the library and propose changes.

- Read tools are always on.
- Propose tools create changesets that change nothing until you approve them in the web UI or with
  `bmk changeset`. There is no apply tool: approval stays with you.
- Single-item write tools (add, edit, keep, star, trash) exist only with BMK_MCP_ALLOW_WRITES=true.
  Deletes are soft (Trash). No raw SQL.

Run with `bmk mcp` (stdio). Register in Claude Code:  claude mcp add bmk -- <path to bmk> mcp
"""

from contextlib import contextmanager
from typing import Any

from . import changesets, dedupe
from . import db as dbmod
from .config import Config, get_config
from .service import Conflict, Library, NotFound

INSTRUCTIONS = (
    "A local bookmarks library. Use search_bookmarks / list_bookmarks / get_bookmark to read it. To change "
    "categories or merge duplicates, use the propose_* tools: they create a changeset the user reviews and "
    "approves; nothing changes until then. Tell the user the changeset id so they can review it."
)


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row.get("title"),
        "url": row.get("url"),
        "domain": row.get("domain"),
        "saved_at": row.get("saved_at"),
        "list": "trash" if row.get("deleted_at") else row.get("status"),
        "starred": bool(row.get("starred")),
        "category": row.get("category"),
        "description": (row.get("description") or "")[:240] or None,
        "remarks": row.get("remarks"),
    }


def build_server(config: Config | None = None):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    config = config or get_config()
    server = MCPServer(name="bmk", instructions=INSTRUCTIONS)

    @contextmanager
    def library():
        lib = Library(dbmod.connect(config.db_path), config, actor="mcp")
        try:
            yield lib
        except (ValueError, NotFound, Conflict) as exc:  # the client sees the message only for a ToolError
            raise ToolError(str(exc)) from exc
        finally:
            lib.close()

    def category_id(lib: Library, name: str | None) -> int | None:
        if name is None or name.strip().lower() in ("", "none"):
            return None
        found = lib.category_by_name(name)
        if found is None:
            raise ValueError(f"no category named '{name}'; call list_categories (or propose_new_categories)")
        return found["id"]

    # ------------------------------------------------------------------ read

    @server.tool()
    def search_bookmarks(query: str, limit: int = 20, view: str = "active", category: str | None = None) -> list[dict[str, Any]]:
        """Full-text search over titles, summaries, page text, remarks and URLs. view: active (default), inbox,
        kept, starred, trash, all. category: a category name, or 'none' for uncategorised."""
        with library() as lib:
            cat = "none" if category and category.lower() == "none" else category_id(lib, category)
            page = lib.browse(view=view, q=query, category=cat, limit=max(1, min(limit, 100)), sort="saved_at")
            return [_brief(r) for r in page["items"]]

    @server.tool()
    def list_bookmarks(view: str = "inbox", category: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """A page of bookmarks, newest first. view: inbox (default), kept, starred, active, trash, date_unknown,
        unprocessed, failed. category: a name or 'none'."""
        with library() as lib:
            cat = "none" if category and category.lower() == "none" else category_id(lib, category)
            page = lib.browse(view=view, category=cat, limit=max(1, min(limit, 200)), offset=max(0, offset))
            return {"total": page["total"], "offset": page["offset"], "items": [_brief(r) for r in page["items"]]}

    @server.tool()
    def get_bookmark(bookmark_id: int) -> dict[str, Any]:
        """One bookmark in full: URLs, sources (devices/files), dates, remarks, summary, the start of the page
        text, merges and history."""
        with library() as lib:
            b = lib.get(bookmark_id)
            cat = lib.conn.execute("SELECT name FROM categories WHERE id=?", (b["category_id"],)).fetchone() if b["category_id"] else None
            return {
                **_brief({**b, "category": cat[0] if cat else None}),
                "canonical_url": b["url_canonical"],
                "saved_at_source": b["saved_at_source"],
                "category_assigned_by": b["category_assigned_by"],
                "site_name": b["site_name"],
                "author": b["author"],
                "page_text_start": (b["content_text"] or "")[:2000] or None,
                "fetch_status": b["fetch_status"],
                "sources": [{k: s[k] for k in ("source_type", "source_ref", "locator", "first_seen_at")} for s in b["sources"]],
                "merges": b["merges"],
            }

    @server.tool()
    def list_categories() -> dict[str, Any]:
        """Categories with descriptions and bookmark counts."""
        with library() as lib:
            return {
                "categories": [{k: c[k] for k in ("id", "name", "description", "bookmarks")} for c in lib.categories()],
                "uncategorised": lib.uncategorised_count(),
            }

    @server.tool()
    def find_duplicates(min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """Groups of bookmarks that look like the same page, with confidence and the reason."""
        with library() as lib:
            dedupe.scan(lib)
            return [
                {
                    "ids": c["ids"],
                    "confidence": c["confidence"],
                    "reason": c["pairs"][0]["rationale"],
                    "suggested_keep": c["suggested"],
                    "bookmarks": [{"id": b["id"], "title": b["title"], "url": b["url"]} for b in c["bookmarks"]],
                }
                for c in dedupe.clusters(lib)
                if c["confidence"] >= min_confidence
            ]

    @server.tool()
    def list_changesets(status: str | None = None) -> list[dict[str, Any]]:
        """Proposals and their state (pending, applied, …)."""
        with library() as lib:
            return [{k: cs[k] for k in ("id", "kind", "author", "status", "summary", "counts", "created_at")} for cs in changesets.list_(lib, status)]

    @server.tool()
    def get_changeset(changeset_id: int) -> dict[str, Any]:
        """One proposal with its items and their decisions."""
        with library() as lib:
            return changesets.get(lib, changeset_id)

    # ------------------------------------------------------------------ propose

    @server.tool()
    def propose_categories(assignments: list[dict[str, Any]], summary: str | None = None) -> dict[str, Any]:
        """Propose categories for bookmarks: [{"bookmark_id": 12, "category": "AI Agents", "rationale": "…",
        "confidence": 0.9}]. Categories must exist (list_categories); 'none' clears one. Bookmarks whose category
        the user set are refused. The whole batch is rejected if any item is invalid. Nothing changes until the
        user approves the changeset."""
        if not assignments:
            raise ToolError("no assignments")
        with library() as lib:
            items = []
            for a in assignments:
                bid = int(a["bookmark_id"])
                b = lib.conn.execute("SELECT category_id, category_assigned_by, deleted_at, merged_into_id FROM bookmarks WHERE id=?", (bid,)).fetchone()
                if b is None or b["merged_into_id"]:
                    raise ValueError(f"bookmark {bid} does not exist")
                if b["category_assigned_by"] == "user":
                    raise ValueError(f"bookmark {bid}: the user set its category; leave it")
                new = category_id(lib, a.get("category"))
                if new != b["category_id"]:
                    items.append((bid, b["category_id"], new, a.get("confidence"), a.get("rationale")))
            if not items:
                return {"changeset_id": None, "message": "every bookmark already has that category"}
            with dbmod.transaction(lib.conn):
                cs_id = changesets.create(lib, "agent", author="mcp", summary=summary or f"{len(items)} category changes proposed via MCP")
                for bid, old, new, conf, why in items:
                    changesets.add_item(lib, cs_id, "set_category", bookmark_id=bid, old_value=old, new_value=new,
                                        confidence=float(conf) if conf is not None else None, rationale=why)  # fmt: skip
            return {"changeset_id": cs_id, "items": len(items), "message": "Waiting for the user's approval."}

    @server.tool()
    def propose_new_categories(categories: list[dict[str, str]], summary: str | None = None) -> dict[str, Any]:
        """Propose new categories: [{"name": "Home & Family", "description": "one sentence on what belongs"}].
        The user reviews and approves them."""
        with library() as lib:
            seen = set()
            for c in categories:
                name = " ".join(str(c.get("name", "")).split())
                if not name or not str(c.get("description", "")).strip():
                    raise ValueError("every category needs a name and a description")
                if lib.category_by_name(name) or name.casefold() in seen:
                    raise ValueError(f"category '{name}' already exists or is repeated")
                seen.add(name.casefold())
            with dbmod.transaction(lib.conn):
                cs_id = changesets.create(lib, "taxonomy", author="mcp", summary=summary or f"{len(categories)} categories proposed via MCP")
                for c in categories:
                    changesets.add_item(lib, cs_id, "add_category", new_value={"name": " ".join(c["name"].split()), "description": c["description"].strip()})
            return {"changeset_id": cs_id, "items": len(categories), "message": "Waiting for the user's approval."}

    @server.tool()
    def propose_merge(keep_id: int, other_ids: list[int], rationale: str) -> dict[str, Any]:
        """Propose merging duplicates into the bookmark to keep (sources, remarks and tags are combined; it can
        be undone). Nothing changes until the user approves."""
        with library() as lib:
            for bid in [keep_id, *other_ids]:
                row = lib.conn.execute("SELECT merged_into_id FROM bookmarks WHERE id=?", (bid,)).fetchone()
                if row is None or row["merged_into_id"]:
                    raise ValueError(f"bookmark {bid} does not exist or was already merged")
            others = [i for i in dict.fromkeys(other_ids) if i != keep_id]
            if not others:
                raise ValueError("other_ids must name at least one other bookmark")
            with dbmod.transaction(lib.conn):
                cs_id = changesets.create(lib, "agent", author="mcp", summary=f"Merge {', '.join(map(str, others))} into {keep_id}")
                for bid in others:
                    changesets.add_item(lib, cs_id, "merge", bookmark_id=bid, target_id=keep_id, rationale=rationale)
            return {"changeset_id": cs_id, "items": len(others), "message": "Waiting for the user's approval."}

    # ------------------------------------------------------------------ single-item writes (opt-in)

    if config.mcp_allow_writes:

        @server.tool()
        def add_bookmark(url: str, title: str | None = None, remarks: str | None = None) -> dict[str, Any]:
            """Save a URL to the Inbox."""
            with library() as lib:
                outcome, bid = lib.add(url, title=title, remarks=remarks)
                return {"outcome": outcome, "id": bid}

        @server.tool()
        def update_bookmark(bookmark_id: int, title: str | None = None, remarks: str | None = None) -> dict[str, Any]:
            """Edit one bookmark's title or remarks (the edit is locked against automatic changes)."""
            with library() as lib:
                return _brief(lib.update(bookmark_id, title=title, remarks=remarks))

        @server.tool()
        def triage_bookmark(bookmark_id: int, action: str) -> dict[str, Any]:
            """One bookmark: action 'keep', 'star', 'unstar', 'inbox' or 'trash' (soft delete, restorable)."""
            with library() as lib:
                if action == "trash":
                    lib.delete(bookmark_id)
                    return {"id": bookmark_id, "list": "trash"}
                fields = {"keep": {"status": "kept"}, "star": {"starred": True}, "unstar": {"starred": False}, "inbox": {"status": "inbox"}}
                if action not in fields:
                    raise ValueError("action must be keep, star, unstar, inbox or trash")
                return _brief(lib.update(bookmark_id, **fields[action]))

    return server


def main() -> None:
    build_server().run("stdio")
