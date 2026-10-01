"""Derived search indexes: FTS5 tables and embedding vectors for current chunks and statements."""

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Sequence

import numpy as np

EMBED_MODEL = "BAAI/bge-m3"
MAX_TOKENS = 1024  # longer chunks are embedded by their first 1024 tokens; FTS still covers the full text
BATCH = 16
FTS_TOKENIZER = "unicode61 remove_diacritics 2"

Embed = Callable[[Sequence[str]], np.ndarray]  # texts -> (n, dim) float32, L2-normalised

CURRENT_VERSIONS = """
SELECT v.id FROM document_versions v
WHERE v.id = (SELECT w.id FROM document_versions w WHERE w.document_id = v.document_id
              ORDER BY w.last_checked_at DESC, w.fetched_at DESC LIMIT 1)
"""


def load_model(name: str = EMBED_MODEL, offline: bool = False) -> Embed:
    """Load the sentence-transformers model; offline=True never touches the network (the MCP server)."""
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"  # no update checks or telemetry from the MCP server
    from sentence_transformers import SentenceTransformer  # heavy import, only when embedding

    model = SentenceTransformer(name, local_files_only=offline)
    model.max_seq_length = MAX_TOKENS

    def embed(texts: Sequence[str]) -> np.ndarray:
        vectors = model.encode(list(texts), batch_size=BATCH, normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(vectors, dtype="<f4")

    return embed


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


def chunk_texts(conn: sqlite3.Connection) -> dict[str, str]:
    """Text embedded per current chunk: document title, section and headings give the chunk its context."""
    rows = conn.execute(
        f"""SELECT c.id, d.title, c.section_ref, c.heading_path, c.text FROM chunks c
            JOIN document_versions v ON v.id = c.version_id JOIN documents d ON d.id = v.document_id
            WHERE c.version_id IN ({CURRENT_VERSIONS})"""  # noqa: S608 - constant subquery
    ).fetchall()
    return {
        chunk_id: f"{title}\n{ref} {' > '.join(json.loads(headings))}\n{text}"
        for chunk_id, title, ref, headings, text in rows
    }


def statement_texts(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        f"""SELECT s.id, s.summary FROM statements s JOIN chunks c ON c.id = s.chunk_id
            WHERE c.version_id IN ({CURRENT_VERSIONS})"""  # noqa: S608 - constant subquery
    ).fetchall()
    return dict(rows)


def build_fts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Rebuild both FTS5 tables from the current versions; returns (chunks, statements) indexed."""
    with conn:
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
        conn.execute("DROP TABLE IF EXISTS statements_fts")
        conn.execute(
            f"CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, heading, text, tokenize='{FTS_TOKENIZER}')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE statements_fts USING fts5(statement_id UNINDEXED, summary, verbatim_quote, "
            f"tokenize='{FTS_TOKENIZER}')"
        )
        chunks = conn.execute(
            f"""INSERT INTO chunks_fts (chunk_id, heading, text)
                SELECT c.id, c.section_ref || ' ' || (SELECT group_concat(value, ' ') FROM json_each(c.heading_path)),
                       c.text
                FROM chunks c WHERE c.version_id IN ({CURRENT_VERSIONS})"""  # noqa: S608 - constant subquery
        ).rowcount
        statements = conn.execute(
            f"""INSERT INTO statements_fts (statement_id, summary, verbatim_quote)
                SELECT s.id, s.summary, s.verbatim_quote FROM statements s JOIN chunks c ON c.id = s.chunk_id
                WHERE c.version_id IN ({CURRENT_VERSIONS})"""  # noqa: S608 - constant subquery
        ).rowcount
    return chunks, statements


def sync_vectors(
    conn: sqlite3.Connection,
    kind: str,
    texts: dict[str, str],
    model: str,
    embed: Embed,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[int, int]:
    """Embed items whose text is new or changed and drop vectors of items gone; returns (embedded, removed)."""
    known = dict(conn.execute("SELECT item_id, text_sha256 FROM vectors WHERE kind = ? AND model = ?", (kind, model)))
    shas = {item: hashlib.sha256(text.encode()).hexdigest() for item, text in texts.items()}
    todo = [item for item, sha in shas.items() if known.get(item) != sha]
    gone = [item for item in known if item not in texts]
    with conn:
        conn.executemany(
            "DELETE FROM vectors WHERE kind = ? AND item_id = ? AND model = ?", [(kind, i, model) for i in gone]
        )
    step = BATCH * 8
    for start in range(0, len(todo), step):
        batch = todo[start : start + step]
        vectors = embed([texts[item] for item in batch])
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO vectors (kind, item_id, model, text_sha256, vector) VALUES (?, ?, ?, ?, ?)",
                [(kind, item, model, shas[item], to_blob(v)) for item, v in zip(batch, vectors, strict=True)],
            )
        progress(f"{kind} vectors {min(start + step, len(todo))}/{len(todo)}")
    return len(todo), len(gone)
