"""Load and validate the domain definition (domain.yaml): the vocabulary one knowledge base is built on."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SLUG_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")
FIELDS = frozenset({"name", "instructions", "doc_types", "tags", "modalities", "topics"})


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
class Domain:
    name: str
    instructions: str  # MCP server instructions shown to the agent
    doc_types: tuple[str, ...]  # most authoritative first; orders kb_topic results
    tags: tuple[str, ...]  # facet values for sources and statements, e.g. distances or audiences
    modalities: tuple[Modality, ...]  # strongest first; orders kb_topic results
    topics: tuple[Topic, ...]


def load(path: Path) -> Domain:
    """Parse and validate domain.yaml; raise ConfigError listing every problem found."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")

    problems = [f"unknown field {k!r}" for k in sorted(raw.keys() - FIELDS, key=str)]
    problems += [
        f"{k} must be a non-empty string"
        for k in ("name", "instructions")
        if not isinstance(raw.get(k), str) or not raw[k].strip()
    ]
    for key in ("doc_types", "tags"):
        problems += _slug_list_problems(key, raw.get(key))
    problems += _entries_problems("modalities", raw.get("modalities"), ("id", "description"))
    problems += _entries_problems("topics", raw.get("topics"), ("id", "label", "description"))
    if problems:
        raise ConfigError("\n".join(f"{path}: {p}" for p in problems))
    return Domain(
        name=raw["name"].strip(),
        instructions=" ".join(raw["instructions"].split()),
        doc_types=tuple(raw["doc_types"]),
        tags=tuple(raw["tags"]),
        modalities=tuple(Modality(m["id"], m["description"]) for m in raw["modalities"]),
        topics=tuple(Topic(t["id"], t["label"], t["description"]) for t in raw["topics"]),
    )


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
