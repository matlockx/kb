"""Load and validate the domain definition (domain.yaml): the vocabulary one knowledge base is built on."""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

SLUG_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")
FIELDS = frozenset(
    {"name", "instructions", "doc_types", "tags", "modalities", "topics", "scopes", "availability", "publishers"}
)
DOC_TYPE_FIELDS = frozenset({"id", "binding", "note"})
PUBLISHER_FIELDS = frozenset({"name", "domains", "official"})
AVAILABILITY_FIELDS = frozenset({"tags", "values", "unknown"})
DOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
PARTIAL_DATE_RE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")


class ConfigError(ValueError):
    """A registry or configuration file is invalid; the message lists every problem found."""


@dataclass(frozen=True)
class Modality:
    id: str
    description: str


@dataclass(frozen=True)
class Topic:
    id: str
    label: str
    description: str


@dataclass(frozen=True)
class Publisher:
    """Who publishes sources, and under which domains their URLs may be; sources.yaml names one by its name."""

    name: str
    domains: tuple[str, ...]  # a source URL's host is one of these or a subdomain of one
    official: bool  # whether the publisher issues the texts itself (a regulator, a legislature), not a secondary source


@dataclass(frozen=True)
class Availability:
    """Whether each listed tag is available per scope, e.g. whether a product can be licensed in a jurisdiction."""

    tags: tuple[str, ...]  # tags every scope states a value for, a subset of the domain tags
    values: tuple[str, ...]  # the values a scope states with a backing source, e.g. licensed, prohibited
    unknown: str  # the value stated without a source, until one backs another value


@dataclass(frozen=True)
class Domain:
    name: str
    instructions: str  # MCP server instructions shown to the agent
    doc_types: tuple[str, ...]  # most authoritative first; orders kb_topic results
    tags: tuple[str, ...]  # facet values for sources and statements, e.g. distances or audiences
    modalities: tuple[Modality, ...]  # strongest first; orders kb_topic results
    topics: tuple[Topic, ...]
    non_binding: frozenset[str] = frozenset()  # doc types whose sections are not binding text, e.g. case law
    doc_type_notes: dict[str, str] = field(default_factory=dict)  # doc type -> note shown on its sections
    scope_label: str | None = None  # singular noun for the scope facet, e.g. jurisdiction; None without scopes
    availability: Availability | None = None  # only with scopes
    publishers: tuple[Publisher, ...] = ()  # declared publishers; empty in a domain written before they existed


def load(path: Path) -> Domain:
    """Parse and validate domain.yaml; raise ConfigError listing every problem found."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse(text, str(path))


def parse(text: str, origin: str) -> Domain:
    """Validate the text of a domain.yaml; origin prefixes every problem in the ConfigError."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{origin}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{origin}: expected a mapping")

    problems = [f"unknown field {k!r}" for k in sorted(raw.keys() - FIELDS, key=str)]
    problems += [
        f"{k} must be a non-empty string"
        for k in ("name", "instructions")
        if not isinstance(raw.get(k), str) or not raw[k].strip()
    ]
    doc_types = raw.get("doc_types")
    problems += _slug_list_problems(
        "doc_types", [_doc_type_id(v) for v in doc_types] if isinstance(doc_types, list) else doc_types
    )
    problems += _slug_list_problems("tags", raw.get("tags"))
    problems += _doc_type_problems(raw.get("doc_types"))
    problems += _entries_problems("modalities", raw.get("modalities"), ("id", "description"))
    problems += _entries_problems("topics", raw.get("topics"), ("id", "label", "description"))
    problems += _scope_problems(raw)
    problems += _publisher_problems(raw.get("publishers"))
    if problems:
        raise ConfigError("\n".join(f"{origin}: {p}" for p in problems))
    spec = raw.get("availability")
    return Domain(
        name=raw["name"].strip(),
        instructions=" ".join(raw["instructions"].split()),
        doc_types=tuple(_doc_type_id(d) for d in raw["doc_types"]),
        tags=tuple(raw["tags"]),
        modalities=tuple(Modality(m["id"], m["description"]) for m in raw["modalities"]),
        topics=tuple(Topic(t["id"], t["label"], t["description"]) for t in raw["topics"]),
        non_binding=frozenset(d["id"] for d in raw["doc_types"] if isinstance(d, dict) and d.get("binding") is False),
        doc_type_notes={
            d["id"]: " ".join(d["note"].split()) for d in raw["doc_types"] if isinstance(d, dict) and "note" in d
        },
        scope_label=raw["scopes"]["label"].strip() if raw.get("scopes") else None,
        availability=Availability(tuple(spec["tags"]), tuple(spec["values"]), spec["unknown"]) if spec else None,
        publishers=tuple(
            Publisher(p["name"].strip(), tuple(p["domains"]), p["official"]) for p in raw.get("publishers") or ()
        ),
    )


def is_partial_date(value: object) -> bool:
    """Whether value is a real date written as YYYY, YYYY-MM or YYYY-MM-DD."""
    if not isinstance(value, str) or not PARTIAL_DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(f"{value}-01-01"[:10])
    except ValueError:
        return False
    return True


def _doc_type_id(value: object) -> object:
    """The id of a doc_types entry: the string itself, or the id of a mapping entry."""
    return value.get("id") if isinstance(value, dict) else value


def _doc_type_problems(values: object) -> list[str]:
    """Mapping entries of doc_types: {id, binding?, note?}; binding false needs a note saying how to read the type."""
    problems = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        where = f"doc_types entry {value.get('id')!r}"
        if unknown := sorted(value.keys() - DOC_TYPE_FIELDS, key=str):
            problems.append(f"{where}: unknown fields {unknown}")
        if "binding" in value and not isinstance(value["binding"], bool):
            problems.append(f"{where}: binding must be true or false")
        note = value.get("note")
        if "note" in value and (not isinstance(note, str) or not note.strip()):
            problems.append(f"{where}: note must be a non-empty string")
        elif value.get("binding") is False and "note" not in value:
            problems.append(f"{where}: binding: false needs a note saying how to read it")
    return problems


def _scope_problems(raw: dict[object, object]) -> list[str]:
    scopes, spec = raw.get("scopes"), raw.get("availability")
    problems = []
    if scopes is not None and (
        not isinstance(scopes, dict)
        or set(scopes) != {"label"}
        or not isinstance(scopes["label"], str)
        or not scopes["label"].strip()
    ):
        problems.append("scopes must be a mapping with exactly label, e.g. {label: jurisdiction}")
    if spec is None:
        return problems
    if scopes is None:
        return [*problems, "availability needs scopes"]
    if not isinstance(spec, dict) or set(spec) != AVAILABILITY_FIELDS:
        return [*problems, f"availability must be a mapping with exactly {sorted(AVAILABILITY_FIELDS)}"]
    for key in ("tags", "values"):
        problems += _slug_list_problems(f"availability.{key}", spec[key])
    tags = raw.get("tags")
    if isinstance(spec["tags"], list) and isinstance(tags, list) and (extra := sorted(set(spec["tags"]) - set(tags))):
        problems.append(f"availability.tags {extra} are not in tags")
    unknown = spec["unknown"]
    if not isinstance(unknown, str) or not SLUG_RE.fullmatch(unknown):
        problems.append("availability.unknown must be one lowercase value, e.g. unknown")
    elif isinstance(spec["values"], list) and unknown in spec["values"]:
        problems.append("availability.unknown must not be one of availability.values")
    return problems


def _slug_list_problems(key: str, values: object) -> list[str]:
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        return [f"{key} must be a non-empty list of strings"]
    hint = "must be lowercase letters and digits joined by - or _"
    problems = [f"{key}: {v!r} {hint}" for v in values if not SLUG_RE.fullmatch(v)]
    if len(set(values)) != len(values):
        problems.append(f"{key} contains duplicates")
    return problems


def _entries_problems(key: str, entries: object, fields: tuple[str, ...]) -> list[str]:
    if not isinstance(entries, list) or not entries:
        return [f"{key} must be a non-empty list"]
    problems, seen = [], set()
    for number, entry in enumerate(entries, start=1):
        if (
            not isinstance(entry, dict)
            or set(entry) != set(fields)
            or not all(isinstance(entry[k], str) and entry[k].strip() for k in fields)
        ):
            problems.append(f"{key} entry {number}: needs exactly {', '.join(fields)} as non-empty strings")
            continue
        if not SLUG_RE.fullmatch(entry["id"]):
            problems.append(f"{key} entry {number}: id must be lowercase letters and digits joined by - or _")
        elif entry["id"] in seen:
            problems.append(f"{key}: duplicate id {entry['id']!r}")
        seen.add(entry["id"])
    return problems


def _publisher_problems(values: object) -> list[str]:
    """publishers is optional; each entry is {name, domains, official} with name unique and domains hostnames."""
    if values is None:
        return []
    if not isinstance(values, list) or not values:
        return ["publishers must be a non-empty list"]
    problems, seen = [], set()
    for number, value in enumerate(values, start=1):
        where = f"publishers entry {number}"
        if not isinstance(value, dict) or set(value) != PUBLISHER_FIELDS:
            problems.append(f"{where}: needs exactly {', '.join(sorted(PUBLISHER_FIELDS))}")
            continue
        name, hosts = value["name"], value["domains"]
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where}: name must be a non-empty string")
        elif name.strip() in seen:
            problems.append(f"{where}: duplicate name {name.strip()!r}")
        else:
            seen.add(name.strip())
        if not isinstance(value["official"], bool):
            problems.append(f"{where}: official must be true or false")
        if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) for h in hosts):
            problems.append(f"{where}: domains must be a non-empty list of host names, e.g. [example.org]")
        else:
            problems += [
                f"{where}: {h!r} is not a lowercase host name such as example.org"
                for h in hosts
                if not DOMAIN_RE.fullmatch(h)
            ]
    return problems
