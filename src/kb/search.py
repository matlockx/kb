"""Read-only queries behind the MCP tools and `kb eval`: hybrid search, section lookup, topic listing, sources."""

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from kb.domain import Domain
from kb.index import CURRENT_VERSIONS, EMBED_MODEL, Embed, from_blob

# Reciprocal rank fusion constant: 10 rather than the usual 60 so a first place in one ranking outweighs middling
# places in several; sections without statements appear in the section rankings only, and with 60 the consensus
# of the other rankings buried them. Tuned on a regulatory golden set; re-tune on eval/golden.yaml per domain.
RRF_K = 10
SECTION_VECTOR_WEIGHT = 3.0  # meaning of the section text is the strongest single signal
# The keyword ranking counts half: in a multilingual corpus an English query matches foreign-language sections only
# on common words, while the multilingual vectors match their meaning. Keywords still decide on rare exact terms
# such as an acronym or a section number.
KEYWORD_WEIGHT = 0.5
STOPWORDS = frozenset(
    {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "has", "have", "how", "in",
    "is", "it", "may", "must", "of", "on", "or", "should", "the", "their", "there", "to", "under", "what", "when",
    "where", "which", "who", "whom", "why", "will", "with",
    }
)  # fmt: skip
CANDIDATES = 200  # hits taken from each ranking before fusion


class QueryError(ValueError):
    """A request the caller can fix; the message says how."""


@dataclass
class Searcher:
    """Holds the embedding model and vector matrices between calls; reloads vectors when the database changes."""

    embed_loader: Callable[[], Embed]
    model: str = EMBED_MODEL
    _embed: Embed | None = None
    _vectors: dict[str, tuple[list[str], np.ndarray]] = field(default_factory=dict)
    _stamp: tuple[int, int] | None = None

    def vectors(self, conn: sqlite3.Connection, kind: str) -> tuple[list[str], np.ndarray]:
        # INSERT OR REPLACE gives a re-embedded row a new rowid, so (count, max rowid) changes on every re-index.
        stamp = conn.execute(
            "SELECT count(*), coalesce(max(rowid), 0) FROM vectors WHERE model = ?", (self.model,)
        ).fetchone()
        if stamp != self._stamp:
            self._vectors, self._stamp = {}, stamp
        if kind not in self._vectors:
            rows = conn.execute(
                "SELECT item_id, vector FROM vectors WHERE kind = ? AND model = ?", (kind, self.model)
            ).fetchall()
            matrix = np.vstack([from_blob(v) for _, v in rows]) if rows else np.zeros((0, 1), dtype="<f4")
            self._vectors[kind] = ([i for i, _ in rows], matrix)
        return self._vectors[kind]

    def embed(self, text: str) -> np.ndarray:
        if self._embed is None:
            self._embed = self.embed_loader()
        return self._embed([text])[0]


def _fts_query(text: str) -> str | None:
    """Every word as a quoted prefix term joined with OR, so user text cannot inject FTS5 syntax; stopwords left out."""
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1 and w not in STOPWORDS]
    if not words:
        return None
    return " OR ".join(f'"{w}"*' if len(w) >= 4 else f'"{w}"' for w in dict.fromkeys(words))


def _allowed_chunks(conn: sqlite3.Connection, tags: list[str] | None, topics: list[str] | None) -> set[str]:
    sql = f"""SELECT c.id FROM chunks c JOIN document_versions v ON v.id = c.version_id
              JOIN documents d ON d.id = v.document_id WHERE c.version_id IN ({CURRENT_VERSIONS})"""  # noqa: S608
    args: list[str] = []
    if tags:
        marks = ",".join("?" * len(tags))
        sql += f" AND EXISTS (SELECT 1 FROM json_each(d.tags) WHERE value IN ({marks}))"  # noqa: S608 - placeholders
        args += tags
    if topics:
        marks = ",".join("?" * len(topics))
        sql += " AND EXISTS (SELECT 1 FROM statements s JOIN statement_topics t ON t.statement_id = s.id"
        sql += f" WHERE s.chunk_id = c.id AND t.topic_id IN ({marks}))"
        args += topics
    return {row[0] for row in conn.execute(sql, args)}


def _require_index(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts'").fetchone():
        raise QueryError("the search index is missing; run `kb index`")


def search(
    conn: sqlite3.Connection,
    searcher: Searcher,
    query: str,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
    limit: int = 10,
) -> dict[str, object]:
    """Rank sections by reciprocal rank fusion of keyword and semantic matches on sections and statements."""
    _require_index(conn)
    _check_topics(conn, topics)
    if not query.strip():
        raise QueryError("query is empty")
    limit = max(1, min(limit, 50))
    allowed = _allowed_chunks(conn, tags, topics)
    # Four rankings fused by reciprocal rank: keywords on section text and on statement summaries (half
    # weight), meaning on section text (triple weight) and on statement summaries. Summaries share one language,
    # so they bridge a query to a section written in another.
    scores: dict[str, float] = {}
    if (fts := _fts_query(query)) is not None:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 0, 2.0, 1.0) LIMIT ?",
            (fts, CANDIDATES * 5),
        ).fetchall()
        _add_rrf(scores, [r[0] for r in rows if r[0] in allowed][:CANDIDATES], KEYWORD_WEIGHT)
        rows = conn.execute(
            """SELECT s.chunk_id FROM statements_fts f JOIN statements s ON s.id = f.statement_id
               WHERE statements_fts MATCH ? ORDER BY bm25(statements_fts, 0, 2.0, 1.0) LIMIT ?""",
            (fts, CANDIDATES * 5),
        ).fetchall()
        _add_rrf(scores, _dedupe(r[0] for r in rows if r[0] in allowed)[:CANDIDATES], KEYWORD_WEIGHT)

    semantic = True
    chunk_ids, chunk_matrix = searcher.vectors(conn, "chunk")
    if len(chunk_ids):
        q = searcher.embed(query)
        _add_rrf(scores, _nearest(chunk_ids, chunk_matrix, q, allowed), SECTION_VECTOR_WEIGHT)
        statement_ids, statement_matrix = searcher.vectors(conn, "statement")
        if len(statement_ids):
            owner = dict(conn.execute("SELECT id, chunk_id FROM statements").fetchall())
            ranked = _nearest(statement_ids, statement_matrix, q, None, CANDIDATES * 5)
            _add_rrf(scores, _dedupe(owner[i] for i in ranked if owner.get(i) in allowed)[:CANDIDATES], 1.0)
    else:
        semantic = False

    ranked = sorted(scores, key=lambda c: -scores[c])
    text_sha = (
        dict(
            conn.execute(
                f"SELECT id, sha256 FROM chunks WHERE id IN ({','.join('?' * len(ranked))})",  # noqa: S608 - placeholders
                ranked,
            ).fetchall()
        )
        if ranked
        else {}
    )
    picked: dict[str, dict[str, object]] = {}  # sha of the section text -> result; documents may repeat sections
    for chunk_id in ranked:
        sha = text_sha[chunk_id]
        if sha in picked:
            picked[sha].setdefault("also_in", []).append(chunk_source(conn, chunk_id))  # type: ignore[union-attr]
        elif len(picked) < limit:
            picked[sha] = _section(conn, chunk_id, excerpt=700)
    top = list(picked.values())
    result: dict[str, object] = {"results": top}
    if not semantic:
        result["note"] = "semantic search unavailable (no vectors); run `kb index`. Results are keyword-only."
    if not top:
        result["note"] = "no matching sections in the knowledge base; do not fill the gap from memory"
    return result


def _add_rrf(scores: dict[str, float], ranking: list[str], weight: float) -> None:
    for rank, item in enumerate(ranking, start=1):
        scores[item] = scores.get(item, 0.0) + weight / (RRF_K + rank)


def chunk_source(conn: sqlite3.Connection, chunk_id: str) -> str:
    doc, ref = conn.execute(
        "SELECT v.document_id, c.section_ref FROM chunks c JOIN document_versions v ON v.id = c.version_id "
        "WHERE c.id = ?",
        (chunk_id,),
    ).fetchone()
    return f"{doc} {ref}"


def _nearest(
    ids: list[str], matrix: np.ndarray, q: np.ndarray, allowed: set[str] | None, n: int = CANDIDATES
) -> list[str]:
    order = np.argsort(-(matrix @ q))
    out = []
    for index in order:
        item = ids[index]
        if allowed is None or item in allowed:
            out.append(item)
            if len(out) == n:
                break
    return out


def _dedupe(items: object) -> list[str]:
    return list(dict.fromkeys(items))  # type: ignore[call-overload]


def _section(conn: sqlite3.Connection, chunk_id: str, excerpt: int | None = None) -> dict[str, object]:
    row = conn.execute(
        """SELECT d.id, d.title, d.publisher, d.language, d.url, d.doc_type, v.sha256, v.fetched_at,
                  v.last_checked_at, c.section_ref, c.heading_path, c.text
           FROM chunks c JOIN document_versions v ON v.id = c.version_id JOIN documents d ON d.id = v.document_id
           WHERE c.id = ?""",
        (chunk_id,),
    ).fetchone()
    source_id, title, publisher, language, url, doc_type, sha, fetched, checked, ref, headings, text = row
    return {
        "source_id": source_id,
        "title": title,
        "publisher": publisher,
        "language": language,
        "doc_type": doc_type,
        "section_ref": ref,
        "headings": json.loads(headings),
        "url": url,
        "version": sha[:12],
        "fetched_at": fetched,
        "last_checked_at": checked,
        "text": text if excerpt is None or len(text) <= excerpt else text[:excerpt] + " …",
        "statements": _statements(conn, "s.chunk_id = ?", [chunk_id]),
    }


def _statements(conn: sqlite3.Connection, where: str, args: list[str]) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""SELECT s.id, s.modality, s.summary, s.verbatim_quote, s.applies_to,
                   (SELECT group_concat(topic_id, ',') FROM statement_topics t WHERE t.statement_id = s.id)
            FROM statements s WHERE {where} ORDER BY s.rowid""",  # noqa: S608 - where is a fixed fragment chosen by the caller
        args,
    ).fetchall()
    return [
        {
            "id": sid,
            "modality": modality,
            "summary": summary,
            "quote": quote,
            "applies_to": json.loads(applies),
            "topics": (topics or "").split(","),
        }
        for sid, modality, summary, quote, applies, topics in rows
    ]


def get_section(conn: sqlite3.Connection, source_id: str, section_ref: str) -> dict[str, object]:
    """Full text of one section of a source's current version; long sections are joined from their parts."""
    version = conn.execute(
        f"SELECT v.id FROM document_versions v WHERE v.document_id = ? AND v.id IN ({CURRENT_VERSIONS})",  # noqa: S608
        (source_id,),
    ).fetchone()
    if version is None:
        known = [r[0] for r in conn.execute("SELECT id FROM documents ORDER BY id")]
        raise QueryError(f"unknown source_id {source_id!r}; known: {', '.join(known)}")
    ref = section_ref.strip()
    rows = conn.execute(
        "SELECT id FROM chunks WHERE version_id = ? AND (section_ref = ? OR section_ref LIKE ? || ' (part %)') "
        "ORDER BY ord",
        (version[0], ref, ref),
    ).fetchall()
    if not rows:
        digits = re.findall(r"\d+", ref)
        near: list[str] = []
        for like in ([f"%{digits[0]}%"] if digits else []) + ["%"]:  # same number, else the document's first refs
            near = [r[0] for r in conn.execute(
                "SELECT section_ref FROM chunks WHERE version_id = ? AND section_ref LIKE ? ORDER BY ord LIMIT 15",
                (version[0], like),
            )]  # fmt: skip
            if near:
                break
        raise QueryError(f"no section {ref!r} in {source_id}; similar refs: {', '.join(near)}")
    parts = [_section(conn, r[0]) for r in rows]
    section = parts[0]
    if len(parts) > 1:
        section["section_ref"] = ref
        section["text"] = "\n".join(str(p["text"]) for p in parts)
        section["statements"] = [s for p in parts for s in p["statements"]]  # type: ignore[attr-defined]
    return section


def topic(
    conn: sqlite3.Connection, domain: Domain, topic_id: str, tags: list[str] | None = None, limit: int = 40
) -> dict[str, object]:
    """Statements tagged with one topic across all current sources, strongest modality and doc type first."""
    _check_topics(conn, [topic_id])
    limit = max(1, min(limit, 200))
    modality_order = {m.id: n for n, m in enumerate(domain.modalities)}
    doc_type_order = {d: n for n, d in enumerate(domain.doc_types)}
    rows = conn.execute(
        f"""SELECT s.id, s.verbatim_quote, s.applies_to, s.modality, d.doc_type, d.id, c.ord, c.section_ref
            FROM statements s JOIN statement_topics t ON t.statement_id = s.id AND t.topic_id = ?
            JOIN chunks c ON c.id = s.chunk_id JOIN document_versions v ON v.id = c.version_id
            JOIN documents d ON d.id = v.document_id
            WHERE c.version_id IN ({CURRENT_VERSIONS})""",  # noqa: S608 - constant subquery
        (topic_id,),
    ).fetchall()
    seen: dict[str, dict[str, object]] = {}
    order: list[tuple[int, int, str, int, str]] = []
    for sid, quote, applies, modality, doc_type, doc, ord_, ref in rows:
        if tags and not set(json.loads(applies)) & set(tags):
            continue
        key = re.sub(r"\W+", " ", quote.lower()).strip()
        if key in seen:  # the same text in several documents
            seen[key]["also_in"].append(f"{doc} {ref}")  # type: ignore[attr-defined]
            continue
        seen[key] = {"id": sid, "source_id": doc, "section_ref": ref, "also_in": []}
        order.append((modality_order.get(modality, 99), doc_type_order.get(doc_type, 99), doc, ord_, key))
    order.sort()
    items = []
    for *_, key in order[:limit]:
        pick = seen[key]
        [statement] = _statements(conn, "s.id = ?", [str(pick["id"])])
        item = {"source_id": pick["source_id"], "section_ref": pick["section_ref"], **statement}
        if pick["also_in"]:
            item["also_in"] = pick["also_in"]
        items.append(item)
    out: dict[str, object] = {
        "topic": topic_id,
        "statements_total": len(order),
        "statements_shown": len(items),
        "statements": items,
    }
    if not order:
        out["note"] = "no statement tagged with this topic in the ingested sources; that is not proof that none exists"
    return out


def sources(conn: sqlite3.Connection) -> dict[str, object]:
    """Ingested documents with their current version and counts, and the topic ids statements are tagged with."""
    docs = conn.execute(
        f"""SELECT d.id, d.title, d.publisher, d.doc_type, d.language, d.url, d.tags,
                   v.sha256, v.fetched_at, v.last_checked_at,
                   (SELECT count(*) FROM chunks c WHERE c.version_id = v.id),
                   (SELECT count(*) FROM statements s JOIN chunks c ON c.id = s.chunk_id WHERE c.version_id = v.id)
            FROM documents d LEFT JOIN document_versions v ON v.document_id = d.id AND v.id IN ({CURRENT_VERSIONS})
            ORDER BY d.id"""  # noqa: S608 - constant subquery
    ).fetchall()
    return {
        "sources": [
            {
                "source_id": d[0],
                "title": d[1],
                "publisher": d[2],
                "doc_type": d[3],
                "language": d[4],
                "url": d[5],
                "tags": json.loads(d[6]),
                "version": d[7][:12] if d[7] else None,
                "fetched_at": d[8],
                "last_checked_at": d[9],
                "sections": d[10],
                "statements": d[11],
            }
            for d in docs
        ],
        "topics": [{"id": i, "label": label} for i, label in conn.execute("SELECT id, label FROM topics ORDER BY id")],
    }


def _check_topics(conn: sqlite3.Connection, topics: list[str] | None) -> None:
    if not topics:
        return
    known = [r[0] for r in conn.execute("SELECT id FROM topics ORDER BY id")]
    if unknown := sorted(set(topics) - set(known)):
        raise QueryError(f"unknown topics {unknown}; use one of: {', '.join(known)}")
