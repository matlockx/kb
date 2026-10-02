"""Load, validate and sync the hand-curated source registry (sources.yaml)."""

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from kb.domain import ConfigError, Domain

STRING_FIELDS = ("id", "publisher", "title", "url", "language", "doc_type")
PATTERN_FIELDS = ("section_pattern", "chapter_pattern", "body_start", "body_end", "skip_sections")
LABEL_FIELDS = ("chapter_label", "first_chapter", "section_label")
ALL_FIELDS = frozenset({*STRING_FIELDS, *PATTERN_FIELDS, *LABEL_FIELDS, "tags", "skip_classes"})
ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LANGUAGE_RE = re.compile(r"[a-z]{2}")


@dataclass(frozen=True)
class Source:
    id: str
    publisher: str
    title: str
    url: str
    language: str
    doc_type: str
    tags: tuple[str, ...]
    section_pattern: str | None = None  # regex with a "ref" group matching a section's first line
    chapter_pattern: str | None = None  # regex with a "ref" group prefixed to section refs that follow
    body_start: str | None = None  # regex matching the first body line; earlier lines are preamble
    body_end: str | None = None  # regex matching the first line after the body; it and later lines are dropped
    chapter_label: str | None = None  # re.Match.expand template replacing the chapter prefix, e.g. "Part \\g<n>"
    first_chapter: str = ""  # prefix used before chapter_pattern first matches
    section_label: str | None = None  # re.Match.expand template for section refs, e.g. "\\g<num> §"
    skip_sections: str | None = None  # regex matched at the start of a section ref; extraction skips those sections
    skip_classes: tuple[str, ...] = ()  # HTML class names whose elements, content included, parse leaves out


def load(path: Path, domain: Domain) -> list[Source]:
    """Parse and validate the registry against the domain; raise ConfigError listing every problem found."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: expected a non-empty list of sources")

    errors: list[str] = []
    found: list[Source] = []
    for number, entry in enumerate(raw, start=1):
        where = f"{path}: entry {number}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        if isinstance(entry.get("id"), str):
            where += f" ({entry['id']})"
        problems = _entry_problems(entry, domain)
        if problems:
            errors.extend(f"{where}: {p}" for p in problems)
            continue
        found.append(
            Source(
                **{k: entry[k] for k in STRING_FIELDS},
                tags=tuple(entry["tags"]),
                **{k: entry.get(k) for k in (*PATTERN_FIELDS, "chapter_label", "section_label")},
                first_chapter=entry.get("first_chapter", ""),
                skip_classes=tuple(entry.get("skip_classes", ())),
            )
        )

    seen: set[str] = set()
    for source in found:
        if source.id in seen:
            errors.append(f"{path}: duplicate id {source.id!r}")
        seen.add(source.id)
    if errors:
        raise ConfigError("\n".join(errors))
    return found


def _entry_problems(entry: dict[object, object], domain: Domain) -> list[str]:
    problems = [f"unknown field {k!r}" for k in sorted(entry.keys() - ALL_FIELDS, key=str)]
    problems += [
        f"{k} must be a non-empty string"
        for k in STRING_FIELDS
        if not isinstance(entry.get(k), str) or not str(entry[k]).strip()
    ]
    if problems:
        return problems

    if not ID_RE.fullmatch(str(entry["id"])):
        problems.append("id must be lowercase letters, digits and single hyphens")
    if not LANGUAGE_RE.fullmatch(str(entry["language"])):
        problems.append("language must be a two-letter ISO 639-1 code")
    if entry["doc_type"] not in domain.doc_types:
        problems.append(f"doc_type must be one of {list(domain.doc_types)} (domain.yaml)")
    url = urlparse(str(entry["url"]))
    if url.scheme != "https" or not url.hostname:
        problems.append("url must be an absolute https URL")

    tags = entry.get("tags")
    if not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags):
        problems.append("tags must be a non-empty list of strings")
    elif unknown := sorted(set(tags) - set(domain.tags)):
        problems.append(f"unknown tags {unknown}; allowed {list(domain.tags)} (domain.yaml)")
    elif len(set(tags)) != len(tags):
        problems.append("tags contains duplicates")

    skip = entry.get("skip_classes")
    if "skip_classes" in entry and (
        not isinstance(skip, list)
        or not skip
        or not all(isinstance(c, str) and c and not any(ch.isspace() for ch in c) for c in skip)
    ):
        problems.append("skip_classes must be a non-empty list of class names without whitespace")

    for key in PATTERN_FIELDS:
        if key in entry:
            problems.extend(_pattern_problems(key, entry[key]))
    problems += [
        f"{k} must be a non-empty string"
        for k in LABEL_FIELDS
        if k in entry and (not isinstance(entry[k], str) or not entry[k].strip())
    ]
    if any(k in entry for k in ("chapter_label", "first_chapter")) and "chapter_pattern" not in entry:
        problems.append("chapter_label and first_chapter need a chapter_pattern")
    if "section_label" in entry and "section_pattern" not in entry:
        problems.append("section_label needs a section_pattern")
    for label, pattern in (("chapter_label", "chapter_pattern"), ("section_label", "section_pattern")):
        problems += _template_problems(label, entry.get(label), entry.get(pattern))
    return problems


def _template_problems(key: str, template: object, pattern: object) -> list[str]:
    """A label must expand against its pattern; parse would otherwise fail on the first match, for every source."""
    if not isinstance(template, str) or not template.strip() or not isinstance(pattern, str):
        return []  # missing or malformed values are reported above
    try:
        compiled = re.compile(pattern)
    except re.error:
        return []  # the invalid pattern is reported by _pattern_problems
    try:
        compiled.sub(template, "")  # compiles the template eagerly, even without a match
    except (re.error, IndexError) as exc:
        return [f"{key} does not fit {key.replace('label', 'pattern')}: {exc}"]
    return []


def _pattern_problems(key: str, pattern: object) -> list[str]:
    if not isinstance(pattern, str) or not pattern:
        return [f"{key} must be a non-empty regex string"]
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return [f"{key} is not a valid regex: {exc}"]
    if key in {"section_pattern", "chapter_pattern"} and "ref" not in compiled.groupindex:
        return [f"{key} needs a named group (?P<ref>...)"]
    return []


def sync(conn: sqlite3.Connection, sources: list[Source]) -> list[str]:
    """Upsert every source into documents; return ids in the database that the registry no longer lists."""
    rows = [(s.id, s.publisher, s.title, s.url, s.language, s.doc_type, json.dumps(list(s.tags))) for s in sources]
    with conn:
        conn.executemany(
            """INSERT INTO documents (id, publisher, title, url, language, doc_type, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                 publisher = excluded.publisher, title = excluded.title, url = excluded.url,
                 language = excluded.language, doc_type = excluded.doc_type, tags = excluded.tags""",
            rows,
        )
    listed = {s.id for s in sources}
    return sorted(row[0] for row in conn.execute("SELECT id FROM documents") if row[0] not in listed)
