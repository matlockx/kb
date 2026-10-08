"""How far a source can be relied on, as a level with its reason: where it is published, what kind of text it is,
and what people who read it concluded.

The level starts from the registry and the domain: a source on a declared publisher's domain is official when that
publisher issues the texts itself and the doc type is binding, otherwise secondary; a source outside every
declared domain is unverified. Reviews of the current version then move it: a vetted verdict raises it one step,
a disputed one overrides everything.
"""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from kb.domain import ConfigError, Domain, Publisher
from kb.sources import Source

LEVELS = ("disputed", "unverified", "secondary", "official")  # lowest first
VERDICTS = ("vetted", "disputed")
CURRENT_VERSION = (  # the version checked most recently, as parse.current_version picks it
    "SELECT v.id FROM document_versions v WHERE v.document_id = d.id "
    "ORDER BY v.last_checked_at DESC, v.fetched_at DESC LIMIT 1"
)


class ReviewError(Exception):
    pass


@dataclass(frozen=True)
class Trust:
    level: str  # one of LEVELS
    reason: str  # why the source has that level, one line
    binding: bool  # whether the doc type is binding and the source no translation


def provenance(domain: Domain, name: str, url: str) -> tuple[Publisher | None, str | None]:
    """(the declared publisher called name, None) when url is on one of its domains, else (it or None, why not)."""
    listed = next((p for p in domain.publishers if p.name == name), None)
    if listed is None:
        return None, f"publisher {name!r} is not declared in domain.yaml publishers"
    host = urlparse(url).hostname or ""
    if not any(host == d or host.endswith(f".{d}") for d in listed.domains):
        return listed, f"host {host!r} is not on a domain of {name!r} ({', '.join(listed.domains)})"
    return listed, None


def check_publishers(domain: Domain, found: list[Source]) -> None:
    """Raise ConfigError unless the domain declares publishers and every source is on its publisher's domains."""
    if not domain.publishers:
        raise ConfigError(
            "domain.yaml declares no publishers; add publishers: [{name, domains, official}] naming each "
            "source's publisher and the domains its URLs are on (see the template)"
        )
    problems = [f"sources.yaml: {s.id}: {why}" for s in found if (why := provenance(domain, s.publisher, s.url)[1])]
    if problems:
        raise ConfigError("\n".join(problems))


def assess(conn: sqlite3.Connection, domain: Domain) -> dict[str, Trust]:
    """The trust of every document in the database, by document id, judged on its current version."""
    reviews = _reviews(conn)
    rows = conn.execute(
        f"SELECT d.id, d.publisher, d.url, d.doc_type, d.translation_of, ({CURRENT_VERSION}) "  # noqa: S608
        "FROM documents d ORDER BY d.id"
    ).fetchall()
    return {
        doc_id: _judge(domain, publisher, url, doc_type, translation_of, reviews.get(version_id, {}))
        for doc_id, publisher, url, doc_type, translation_of, version_id in rows
    }


def current_version(conn: sqlite3.Connection, document_id: str) -> str | None:
    """The id of the version of the document that was checked most recently; None without a fetched version."""
    row = conn.execute(f"SELECT ({CURRENT_VERSION}) FROM documents d WHERE d.id = ?", (document_id,)).fetchone()  # noqa: S608
    return row[0] if row else None


def record(conn: sqlite3.Connection, document_id: str, reviewer: str, verdict: str, note: str) -> str:
    """Store the verdict of reviewer on the current version of the document; returns that version's id.

    A later verdict of the same reviewer on the same version replaces the earlier one in assess. A disputed verdict
    needs a note saying why.
    """
    note = " ".join(note.split())
    if verdict not in VERDICTS:
        raise ReviewError(f"verdict must be one of {', '.join(VERDICTS)}")
    if verdict == "disputed" and not note:
        raise ReviewError("a disputed verdict needs a note saying why")
    if conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
        raise ReviewError(f"unknown source id: {document_id}")
    version = current_version(conn, document_id)
    if version is None:
        raise ReviewError(f"{document_id} has no fetched version; run kb fetch first")
    created = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    with conn:
        conn.execute(
            "INSERT INTO reviews (id, version_id, reviewer, verdict, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, version, reviewer, verdict, note, created),
        )
    return version


def _reviews(conn: sqlite3.Connection) -> dict[str, dict[str, tuple[str, str]]]:
    """version id -> reviewer -> (verdict, note), the latest verdict of each reviewer."""
    try:
        rows = conn.execute("SELECT version_id, reviewer, verdict, note FROM reviews ORDER BY created_at, rowid")
    except sqlite3.OperationalError:  # a database from before reviews existed
        return {}
    found: dict[str, dict[str, tuple[str, str]]] = {}
    for version_id, reviewer, verdict, note in rows:
        found.setdefault(version_id, {})[reviewer] = (verdict, note)
    return found


def _judge(
    domain: Domain,
    publisher: str,
    url: str,
    doc_type: str,
    translation_of: str | None,
    votes: dict[str, tuple[str, str]],
) -> Trust:
    binding = doc_type not in domain.non_binding and not translation_of
    listed, problem = provenance(domain, publisher, url)
    if problem or listed is None:
        level, why = "unverified", problem or ""
    elif listed.official and binding:
        host = urlparse(url).hostname
        level, why = "official", f"{publisher} is an official publisher on {host} and {doc_type} is binding"
    else:
        parts = [] if listed.official else [f"{publisher} is not an official publisher"]
        if not binding:
            parts.append("a translation" if translation_of else f"{doc_type} is not binding")
        level, why = "secondary", "; ".join(parts)
    disputes = [f"{who}: {note}" for who, (verdict, note) in votes.items() if verdict == "disputed"]
    if disputes:
        return Trust("disputed", f"disputed by {'; '.join(disputes)} (otherwise {level}: {why})", binding)
    vetters = [who for who, (verdict, _) in votes.items() if verdict == "vetted"]
    if vetters:
        level = LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)]
        why += f"; vetted by {', '.join(vetters)}"
    return Trust(level, why, binding)
