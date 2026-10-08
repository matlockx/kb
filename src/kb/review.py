"""`kb review`: list the trust of the sources, show one with its reviews, or record a verdict on it."""

import sqlite3
import sys
from pathlib import Path

from kb import db, trust
from kb import domain as domains


def overview(home: Path) -> list[tuple[str, trust.Trust]]:
    """(source id, trust) of every source in the database of the knowledge base in home, from its domain.yaml."""
    defined = domains.load(home / "domain.yaml")
    path = home / db.DEFAULT_PATH
    if not path.exists():
        raise domains.ConfigError(f"{path} does not exist; run kb fetch first")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return list(trust.assess(conn, defined).items())
    finally:
        conn.close()


def run(
    db_path: Path, defined: domains.Domain, source_id: str | None, verdict: str | None, note: str, reviewer: str
) -> int:
    """List every source with its trust (no source_id), show one with its reviews, or record verdict by reviewer.

    Returns 1 when the database or source does not exist or the verdict cannot be recorded.
    """
    if not db_path.exists():
        print(f"{db_path} does not exist; run sources, fetch, parse and extract first", file=sys.stderr)
        return 1
    db.upgrade(db_path)  # adds the reviews table to a database built before it existed
    conn = db.connect(db_path) if verdict else sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        judged = trust.assess(conn, defined)
        if source_id is None:
            for doc_id, found in judged.items():
                print(f"{doc_id:<36} {found.level:<10} {found.reason}")
            return 0
        if source_id not in judged:
            print(f"unknown source id: {source_id}", file=sys.stderr)
            return 1
        if verdict is not None:
            try:
                version = trust.record(conn, source_id, reviewer, verdict, note)
            except trust.ReviewError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(f"{reviewer} {verdict} {source_id} (version {version[:12]})")
            judged = trust.assess(conn, defined)
        _show(conn, source_id, judged[source_id])
    finally:
        conn.close()
    return 0


def _show(conn: sqlite3.Connection, source_id: str, found: trust.Trust) -> None:
    title, publisher, url = conn.execute(
        "SELECT title, publisher, url FROM documents WHERE id = ?", (source_id,)
    ).fetchone()
    print(source_id)
    for label, value in {
        "title": title,
        "publisher": publisher,
        "url": url,
        "trust": f"{found.level}: {found.reason}",
    }.items():
        print(f"  {label:<10} {value}")
    current = trust.current_version(conn, source_id)
    rows = conn.execute(
        "SELECT r.created_at, r.reviewer, r.verdict, r.note, r.version_id FROM reviews r "
        "JOIN document_versions v ON v.id = r.version_id WHERE v.document_id = ? ORDER BY r.created_at, r.rowid",
        (source_id,),
    ).fetchall()
    print(f"  {'reviews':<10} {len(rows) or 'none'}")
    for created, reviewer, verdict, note, version_id in rows:
        earlier = "" if version_id == current else " (an earlier version)"
        print(f"    {created[:10]}  {verdict:<8} {reviewer}{earlier}  {note}".rstrip())
