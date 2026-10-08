"""MCP server over stdio exposing read-only knowledge base tools to coding agents."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from kb import db, search
from kb.domain import Domain
from kb.index import load_model

GROUNDING = (
    " Cite every statement you rely on with its source_id and section_ref. When the tools return nothing for a "
    "question, say that the knowledge base has no answer; do not fill the gap from memory."
)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def build(db_path: Path, domain: Domain, embed_loader: Callable[[], search.Embed] | None = None) -> MCPServer:
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} does not exist; run the kb pipeline first")
    db.upgrade(db_path)  # a database built by an older engine serves; a current one stays untouched (read-only files)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        translated = conn.execute("SELECT 1 FROM documents WHERE translation_of IS NOT NULL LIMIT 1").fetchone()
    finally:
        conn.close()
    searcher = search.Searcher(embed_loader or (lambda: load_model(offline=True)))
    server: MCPServer = MCPServer("kb", instructions=f"{domain.name}: {domain.instructions}{GROUNDING}")
    label = domain.scope_label
    scoped = (
        f" Filter by {label} with scopes ({label} ids; kb_sources lists them); without scopes, a {label} the query "
        "names filters by it, reported as scopes_inferred."
        if label
        else " This knowledge base has no scopes; leave scopes out."
    )
    standing = (
        " A section or statement with binding false is not binding text; its note says how to read it."
        if domain.non_binding or translated
        else ""
    ) + (
        " A translation's statements name the original's section as original_section; cite that." if translated else ""
    )

    def run(query: Callable[[sqlite3.Connection], dict[str, Any]]) -> str:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return json.dumps(query(conn), ensure_ascii=False, indent=1)
        except search.QueryError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        finally:
            conn.close()

    @server.tool(
        annotations=READ_ONLY,
        structured_output=False,
        description="Search the knowledge base by meaning and keywords, in any language.\n\nReturns ranked "
        "sections (source_id, section_ref, text excerpt, url, version, fetched_at) with the statements extracted "
        "from each. Filter with tags and topic ids (kb_sources lists both)." + scoped + " Use kb_get for a "
        "section's full text and kb_topic for every statement on one topic." + standing,
    )
    def kb_search(
        query: str,
        scopes: list[str] | None = None,
        tags: list[str] | None = None,
        topics: list[str] | None = None,
        limit: int = 10,
    ) -> str:
        return run(lambda c: search.search(c, searcher, domain, query, scopes, tags, topics, limit))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def kb_get(source_id: str, section_ref: str) -> str:
        """Full text of one section, with its statements.

        source_id and section_ref are as returned by kb_search or kb_topic. An unknown ref returns similar refs.
        """
        return run(lambda c: search.get_section(c, domain, source_id, section_ref))

    per_scope = (
        f" With scopes, the statements are grouped per {label}, each with its availability"
        + (
            f" ({', '.join(domain.availability.values)} or {domain.availability.unknown} per tag)"
            if domain.availability
            else ""
        )
        + "; an availability with in_force false does not apply yet."
        if label
        else ""
    )

    @server.tool(
        annotations=READ_ONLY,
        structured_output=False,
        description="Every statement tagged with one topic id (kb_sources lists them), each with modality, summary "
        "and verbatim quote, strongest modality and most authoritative document type first. Statements with the "
        "same text in several documents are shown once, with also_in." + per_scope + standing,
    )
    def kb_topic(topic: str, scopes: list[str] | None = None, tags: list[str] | None = None, limit: int = 40) -> str:
        return run(lambda c: search.topic(c, domain, topic, scopes, tags, limit))

    @server.tool(
        annotations=READ_ONLY,
        structured_output=False,
        description="List the ingested documents with version, fetch date, tags, counts and trust (disputed, "
        "unverified, secondary or official, with the reason), and the topic ids."
        + (
            f" Also lists each {label} with its details and availability; scope limits the list to one {label}."
            if label
            else ""
        ),
    )
    def kb_sources(scope: str | None = None) -> str:
        return run(lambda c: search.sources(c, domain, scope))

    return server


def serve(db_path: Path, domain: Domain) -> None:
    build(db_path.resolve(), domain).run("stdio")
