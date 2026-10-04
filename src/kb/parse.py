"""Parse the current version of each document into chunks."""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from kb.chunk import Section, split
from kb.extract import extract
from kb.sources import Source


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class Parsed:
    source_id: str
    version_id: str | None
    sections: list[Section]
    error: str | None = None
    kept: bool = False  # the download is not on this device (a pulled copy); the stored sections stay


def current_version(conn: sqlite3.Connection, document_id: str) -> tuple[str, str, str | None] | None:
    """(version id, raw path, content type) of the version checked most recently."""
    return conn.execute(
        "SELECT id, raw_path, content_type FROM document_versions WHERE document_id = ? "
        "ORDER BY last_checked_at DESC, fetched_at DESC LIMIT 1",
        (document_id,),
    ).fetchone()


def sections_of(path: Path, content_type: str | None, source: Source) -> list[Section]:
    """Split one stored download into the sections parse stores for it."""
    return split(
        extract(path, content_type, source.skip_classes),
        source.section_pattern,
        source.chapter_pattern,
        source.body_start,
        source.chapter_label,
        source.first_chapter,
        source.body_end,
        source.section_label,
    )


def parse_all(conn: sqlite3.Connection, sources: Iterable[Source], raw_dir: Path) -> list[Parsed]:
    """Replace the chunks of each source's current version; a failure leaves that version's chunks untouched."""
    results = []
    for source in sources:
        version = current_version(conn, source.id)
        if version is None:
            results.append(Parsed(source.id, None, [], "not fetched yet"))
            continue
        version_id, raw_path, content_type = version
        if (
            not (raw_dir / raw_path).exists()
            and conn.execute("SELECT 1 FROM chunks WHERE version_id = ? LIMIT 1", (version_id,)).fetchone()
        ):
            results.append(Parsed(source.id, version_id, [], kept=True))
            continue
        try:
            sections = sections_of(raw_dir / raw_path, content_type, source)
            if not sections:
                raise ParseError("no text extracted")
            store(conn, version_id, sections)
        except (OSError, ValueError, ParseError) as exc:
            results.append(Parsed(source.id, version_id, [], str(exc)))
            continue
        results.append(Parsed(source.id, version_id, sections))
    return results


def store(conn: sqlite3.Connection, version_id: str, sections: list[Section]) -> None:
    """Replace a version's chunks; sections identical to the stored ones leave them untouched.

    Raise ParseError when statements cite chunks that would change.
    """
    rows = [(s.ref, json.dumps(list(s.heading_path), ensure_ascii=False), s.text) for s in sections]
    with conn:
        stored = conn.execute(
            "SELECT section_ref, heading_path, text FROM chunks WHERE version_id = ? ORDER BY ord", (version_id,)
        ).fetchall()
        if stored == rows:
            return
        used = conn.execute(
            "SELECT count(*) FROM statements s JOIN chunks c ON c.id = s.chunk_id WHERE c.version_id = ?",
            (version_id,),
        ).fetchone()[0]
        if used:
            raise ParseError(f"{used} statements already cite this version's chunks; not re-chunking")
        conn.execute(
            "DELETE FROM vectors WHERE kind = 'chunk' AND item_id IN (SELECT id FROM chunks WHERE version_id = ?)",
            (version_id,),
        )
        conn.execute("DELETE FROM chunks WHERE version_id = ?", (version_id,))
        conn.executemany(
            "INSERT INTO chunks (id, version_id, ord, section_ref, heading_path, text, sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"{version_id}#{ord_:04d}",
                    version_id,
                    ord_,
                    ref,
                    headings,
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                )
                for ord_, (ref, headings, text) in enumerate(rows)
            ],
        )
