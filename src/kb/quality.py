"""`kb quality`: what has been ingested, machine-checked and can serve as evidence, per source."""

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from kb import db, trust
from kb.domain import Domain
from kb.statements import comparable


@dataclass(frozen=True)
class Row:
    source_id: str
    level: str  # trust.LEVELS
    fetched: bool  # whether the source has a version at all
    sections: int  # of the current version
    covered: int  # sections with at least one statement
    statements: int  # of the current version
    verified: int  # statements whose quote is still found in its section
    evidence: int  # verified statements of a binding source whose trust is official
    checked: str  # last_checked_at of the current version, ISO-8601; '' without a version


def measure(conn: sqlite3.Connection, domain: Domain) -> list[Row]:
    """One row per document, judged on its current version."""
    rows = []
    for doc_id, found in trust.assess(conn, domain).items():
        version_id = trust.current_version(conn, doc_id)
        if version_id is None:
            rows.append(Row(doc_id, found.level, False, 0, 0, 0, 0, 0, ""))
            continue
        checked = conn.execute("SELECT last_checked_at FROM document_versions WHERE id = ?", (version_id,)).fetchone()[
            0
        ]
        texts = dict(conn.execute("SELECT id, text FROM chunks WHERE version_id = ?", (version_id,)))
        quotes = conn.execute(
            "SELECT s.chunk_id, s.verbatim_quote FROM statements s JOIN chunks c ON c.id = s.chunk_id "
            "WHERE c.version_id = ?",
            (version_id,),
        ).fetchall()
        compared: dict[str, str] = {}  # chunk id -> its text as comparable() reads it, made once per section
        verified = 0
        for chunk_id, quote in quotes:
            if chunk_id not in compared:
                compared[chunk_id] = comparable(texts[chunk_id])
            verified += comparable(quote) in compared[chunk_id]
        evidence = verified if found.level == "official" and found.binding else 0
        covered = len({chunk_id for chunk_id, _ in quotes})
        rows.append(Row(doc_id, found.level, True, len(texts), covered, len(quotes), verified, evidence, checked))
    return rows


def run(db_path: Path, domain: Domain) -> int:
    """Print the quality table of the database at db_path; 1 when it does not exist."""
    if not db_path.exists():
        print(f"{db_path} does not exist; run sources, fetch, parse and extract first", file=sys.stderr)
        return 1
    db.upgrade(db_path)  # a database built by an older engine lacks columns and tables the queries read
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = measure(conn, domain)
    finally:
        conn.close()
    head = ("source", "trust", "sections", "covered", "statements", "verified", "evidence")
    print(f"{head[0]:<36} {head[1]:<10} " + " ".join(f"{h:>10}" for h in head[2:]) + "  checked")
    for r in rows:
        figures = (r.sections, r.covered, r.statements, r.verified, r.evidence)
        print(
            f"{r.source_id:<36} {r.level:<10} "
            + " ".join(f"{n:>10}" for n in figures)
            + f"  {r.checked[:10] or 'not fetched'}"
        )
    levels = ", ".join(f"{sum(r.level == level for r in rows)} {level}" for level in reversed(trust.LEVELS))
    print(
        f"{len(rows)} sources ({levels}); {sum(r.sections for r in rows)} sections, "
        f"{sum(r.covered for r in rows)} with statements; {sum(r.statements for r in rows)} statements, "
        f"{sum(r.verified for r in rows)} verified, {sum(r.evidence for r in rows)} evidence"
    )
    print(
        "covered: sections with a statement. verified: the quote is still found in its section (machine check, as "
        "kb eval). evidence: verified statements of a binding source whose trust is official, reviews included. "
        "Quotes rejected at extraction are not stored, so they are not counted."
    )
    return 0
