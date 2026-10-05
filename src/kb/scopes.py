"""Load, validate and sync the scope registry (scopes.yaml): the values of a document-level facet such as
jurisdictions, each with its names, languages, details and, when the domain defines availability, a stated value
per tag backed by a source (e.g. whether a product can be licensed in a jurisdiction)."""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from kb.domain import ConfigError, Domain, is_partial_date
from kb.sources import LANGUAGE_RE, Source

PATH = Path("scopes.yaml")
ID_RE = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
FIELDS = frozenset({"id", "name", "aliases", "languages", "details", "availability"})
ENTRY_FIELDS = frozenset({"status", "source", "effective_from", "note"})


@dataclass(frozen=True)
class Stated:
    """The availability value one scope states for one tag."""

    tag: str
    status: str
    source: str | None = None  # source id backing the status; absent only for the unknown value
    effective_from: str | None = None  # partial date from which the status applies
    note: str | None = None


@dataclass(frozen=True)
class Scope:
    id: str
    name: str
    aliases: tuple[str, ...] = ()  # further names a question may use, matched as whole words
    languages: tuple[str, ...] = ()  # ISO 639-1; a source in another language must be a translation
    details: dict[str, object] = field(default_factory=dict)  # shown verbatim by kb_sources
    availability: tuple[Stated, ...] = ()


def load(path: Path, domain: Domain, sources: list[Source]) -> list[Scope]:
    """Parse the registry and check it against the domain and the sources; raise ConfigError listing every problem."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: expected a non-empty list of {domain.scope_label or 'scope'} entries")
    errors: list[str] = []
    found: list[Scope] = []
    for number, entry in enumerate(raw, start=1):
        where = f"{path}: entry {number}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        if isinstance(entry.get("id"), str):
            where += f" ({entry['id']})"
        if problems := _entry_problems(entry, domain):
            errors.extend(f"{where}: {p}" for p in problems)
            continue
        found.append(_scope(entry, domain))
    seen: set[str] = set()
    for scope in found:
        if scope.id.lower() in seen:
            errors.append(f"{path}: duplicate id {scope.id!r}")
        seen.add(scope.id.lower())
    if not errors:
        errors = [f"{path}: {p}" for p in check(found, sources)]
    if errors:
        raise ConfigError("\n".join(errors))
    return found


def _scope(entry: dict, domain: Domain) -> Scope:
    stated = entry.get("availability") or {}
    return Scope(
        id=entry["id"],
        name=entry["name"].strip(),
        aliases=tuple(a.strip() for a in entry.get("aliases", ())),
        languages=tuple(entry.get("languages", ())),
        details=dict(entry.get("details") or {}),
        availability=tuple(
            Stated(tag, **{k: _text(v) for k, v in stated[tag].items()})
            for tag in (domain.availability.tags if domain.availability else ())
        ),
    )


def _text(value: object) -> str:
    """YAML reads an unquoted 2027-07-01 as a date; every availability field is text."""
    return value.isoformat() if isinstance(value, date) else str(value)


def _entry_problems(entry: dict[object, object], domain: Domain) -> list[str]:
    problems = [f"unknown field {k!r}" for k in sorted(entry.keys() - FIELDS, key=str)]
    if not isinstance(entry.get("id"), str) or not ID_RE.fullmatch(entry["id"]):
        problems.append("id must be letters and digits joined by - or _, e.g. GB")
    if not isinstance(entry.get("name"), str) or not entry["name"].strip():
        problems.append("name must be a non-empty string")
    aliases = entry.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
        problems.append("aliases must be a list of non-empty strings")
    languages = entry.get("languages", [])
    if not isinstance(languages, list) or not all(
        isinstance(lang, str) and LANGUAGE_RE.fullmatch(lang) for lang in languages
    ):
        problems.append("languages must be a list of two-letter ISO 639-1 codes")
    elif len(set(languages)) != len(languages):
        problems.append("languages contains duplicates")
    details = entry.get("details", {})
    if not isinstance(details, dict) or not all(
        isinstance(k, str) and isinstance(v, str | bool | int | float) for k, v in details.items()
    ):
        problems.append("details must be a mapping of names to plain values")
    return problems + _availability_problems(entry, domain)


def _availability_problems(entry: dict[object, object], domain: Domain) -> list[str]:
    spec = domain.availability
    if spec is None:
        return ["availability needs availability in domain.yaml"] if "availability" in entry else []
    stated = entry.get("availability")
    if not isinstance(stated, dict):
        return [f"availability must be a mapping of {list(spec.tags)}"]
    problems = []
    if missing := [t for t in spec.tags if t not in stated]:
        problems.append(f"availability is missing {missing}")
    if extra := sorted(set(stated) - set(spec.tags), key=str):
        problems.append(f"availability has unknown tags {extra}")
    allowed = [*spec.values, spec.unknown]
    for tag in spec.tags:
        value = stated.get(tag)
        if tag not in stated:
            continue
        where = f"availability.{tag}"
        if not isinstance(value, dict):
            problems.append(f"{where}: expected a mapping")
            continue
        problems += [f"{where}: unknown field {k!r}" for k in sorted(value.keys() - ENTRY_FIELDS, key=str)]
        status = value.get("status")
        if status not in allowed:
            problems.append(f"{where}: status must be one of {allowed}")
        for key in ("source", "effective_from", "note"):
            if key in value and not (isinstance(value[key], str | date) and str(value[key]).strip()):
                problems.append(f"{where}: {key} must be a non-empty string")
        if status in spec.values and "source" not in value:
            problems.append(f"{where}: status {status!r} needs a source that backs it")
        if "effective_from" in value and not is_partial_date(_text(value["effective_from"])):
            problems.append(f"{where}: effective_from must be YYYY, YYYY-MM or YYYY-MM-DD")
    return problems


def check(scopes: list[Scope], sources: list[Source]) -> list[str]:
    """Problems that only show when the scope and source registries are read together."""
    by_id = {s.id: s for s in scopes}
    by_source = {s.id: s for s in sources}
    problems = []
    for source in sources:
        scope = by_id.get(str(source.scope))
        if scope is None:
            problems.append(f"source {source.id}: scope {source.scope!r} is not in the registry")
        elif scope.languages and source.translation_of is None and source.language not in scope.languages:
            problems.append(
                f"source {source.id}: language {source.language!r} is not one of {scope.id} languages "
                f"{list(scope.languages)}; a translation needs translation_of pointing at the original"
            )
    for scope in scopes:
        for stated in scope.availability:
            if stated.source is None:
                continue
            where = f"{scope.id} availability.{stated.tag}"
            backing = by_source.get(stated.source)
            if backing is None:
                problems.append(f"{where}: source {stated.source!r} is not in the source registry")
            elif backing.scope != scope.id:
                problems.append(f"{where}: source {stated.source!r} belongs to {backing.scope}")
    return problems


def sync(conn: sqlite3.Connection, scopes: list[Scope]) -> None:
    """Replace the scopes and availability tables with the registry."""
    with conn:
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM scopes")
        conn.executemany(
            "INSERT INTO scopes (id, name, aliases, languages, details) VALUES (?, ?, ?, ?, ?)",
            [
                (s.id, s.name, json.dumps(list(s.aliases)), json.dumps(list(s.languages)), json.dumps(s.details))
                for s in scopes
            ],
        )
        conn.executemany(
            "INSERT INTO availability (scope, tag, status, source_id, effective_from, note) VALUES (?, ?, ?, ?, ?, ?)",
            [(s.id, a.tag, a.status, a.source, a.effective_from, a.note) for s in scopes for a in s.availability],
        )
