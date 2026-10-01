"""MCP server over stdio exposing read-only knowledge base tools to coding agents."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from kb import search
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
    searcher = search.Searcher(embed_loader or (lambda: load_model(offline=True)))
    server: MCPServer = MCPServer("kb", instructions=f"{domain.name}: {domain.instructions}{GROUNDING}")

    def run(query: Callable[[sqlite3.Connection], dict[str, Any]]) -> str:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return json.dumps(query(conn), ensure_ascii=False, indent=1)
        except search.QueryError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        finally:
            conn.close()

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def kb_search(query: str, tags: list[str] | None = None, topics: list[str] | None = None, limit: int = 10) -> str:
        """Search the knowledge base by meaning and keywords, in any language.

        Returns ranked sections (source_id, section_ref, text excerpt, url, version, fetched_at) with the
        statements extracted from each. Filter with tags and topic ids (kb_sources lists both). Use kb_get for a
        section's full text and kb_topic for every statement on one topic.
        """
        return run(lambda c: search.search(c, searcher, query, tags, topics, limit))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def kb_get(source_id: str, section_ref: str) -> str:
        """Full text of one section, with its statements.

        source_id and section_ref are as returned by kb_search or kb_topic. An unknown ref returns similar refs.
        """
        return run(lambda c: search.get_section(c, source_id, section_ref))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def kb_topic(topic: str, tags: list[str] | None = None, limit: int = 40) -> str:
        """Every statement tagged with one topic id (kb_sources lists them), each with modality, summary and
        verbatim quote, strongest modality and most authoritative document type first. Statements with the same
        text in several documents are shown once, with also_in.
        """
        return run(lambda c: search.topic(c, domain, topic, tags, limit))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def kb_sources() -> str:
        """List the ingested documents with version, fetch date, tags and counts, and the topic ids."""
        return run(search.sources)

    return server


def serve(db_path: Path, domain: Domain) -> None:
    build(db_path.resolve(), domain).run("stdio")
