"""Structural audit of the current version of every source in a kb database.

Usage: uv run python scripts/audit_structure.py [data/kb.db] > audit.tsv

Prints one tab-separated row per source with the signals a source audit checks by hand: chunk counts,
preamble size, oversized and tiny chunks, duplicate refs, gaps in the section numbering, table-of-contents
leakage, sections without statements, statements from an outdated prompt, and statements left on a
non-current version.
"""

import itertools
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from kb import domain, statements

COLUMNS = (
    "source", "doc_type", "checked", "age_days", "versions", "chunks", "preamble_chars", "parts", "max_len",
    "tiny", "dup_refs", "gaps", "toc_like", "no_statements", "statements", "old_prompt_statements",
    "stale_version_statements",
)  # fmt: skip
PLAIN_REF = re.compile(r"(?:s\.|Article|Artigo|§|Section)?\s*(\d+)[A-Za-z]?")
TOC_LINE = re.compile(r"(\.{4,}|…{2,})\s*\d+\s*$|^\s*\d+(\.\d+)*\s+\S.{0,60}\s\d{1,3}\s*$")


def number_gaps(refs: list[str]) -> int:
    """Missing integers in the run of plain section numbers ("s. 12", "Article 7", "§ 3", "12"); letters ignored."""
    plain = (PLAIN_REF.fullmatch(r) for r in refs)
    numbers = sorted({int(m.group(1)) for m in plain if m})
    return sum(b - a - 1 for a, b in itertools.pairwise(numbers) if 1 < b - a <= 20)


def toc_like(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) >= 5 and sum(bool(TOC_LINE.search(line)) for line in lines) / len(lines) > 0.5


def main(db: Path) -> None:
    version_now = statements.prompt_version(
        statements.system_prompt(Path("prompts/extract.md"), domain.load(Path("domain.yaml")))
    )
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    now = datetime.now(UTC)
    sys.stdout.write("\t".join(COLUMNS) + "\n")
    for doc_id, doc_type in conn.execute("SELECT id, doc_type FROM documents ORDER BY id").fetchall():
        version = conn.execute(
            "SELECT id, last_checked_at FROM document_versions WHERE document_id = ? "
            "ORDER BY last_checked_at DESC, fetched_at DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        if version is None:
            sys.stdout.write(f"{doc_id}\t{doc_type}\tnot fetched\n")
            continue
        version_id, checked = version
        chunks = conn.execute(
            "SELECT id, section_ref, text FROM chunks WHERE version_id = ? ORDER BY ord", (version_id,)
        ).fetchall()
        refs = [ref for _, ref, _ in chunks]
        body = [(cid, ref, text) for cid, ref, text in chunks if not ref.startswith("(preamble)")]
        with_statements = {row[0] for row in conn.execute(
            "SELECT DISTINCT s.chunk_id FROM statements s JOIN chunks c ON c.id = s.chunk_id WHERE c.version_id = ?",
            (version_id,),
        )}  # fmt: skip
        count, old = conn.execute(
            "SELECT count(*), coalesce(sum(s.prompt_version != ?), 0) FROM statements s "
            "JOIN chunks c ON c.id = s.chunk_id WHERE c.version_id = ?",
            (version_now, version_id),
        ).fetchone()
        stale = conn.execute(
            "SELECT count(*) FROM statements s JOIN chunks c ON c.id = s.chunk_id JOIN document_versions v "
            "ON v.id = c.version_id WHERE v.document_id = ? AND v.id != ?",
            (doc_id, version_id),
        ).fetchone()[0]
        base_refs = [re.sub(r" \(part 1\)$", "", r) for r in refs if not re.search(r" \(part (?!1\))\d+\)$", r)]
        row = (
            doc_id, doc_type, checked[:10], (now - datetime.fromisoformat(checked)).days,
            conn.execute("SELECT count(*) FROM document_versions WHERE document_id = ?", (doc_id,)).fetchone()[0],
            len(chunks), sum(len(t) for _, r, t in chunks if r.startswith("(preamble)")),
            sum("(part " in r for r in refs), max((len(t) for _, _, t in chunks), default=0),
            sum(len(t) < 80 for _, _, t in body),
            len(base_refs) - len(set(base_refs)) + sum(bool(re.search(r" \(\d+\)$", r)) for r in refs),
            number_gaps(refs), sum(toc_like(t) for _, _, t in chunks),
            sum(cid not in with_statements for cid, _, _ in body), count, old, stale,
        )  # fmt: skip
        sys.stdout.write("\t".join(str(v) for v in row) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/kb.db"))
