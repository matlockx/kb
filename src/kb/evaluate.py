"""Golden-set evaluation of kb_search, and a re-check of every stored quote."""

import fnmatch
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from kb import search
from kb.domain import ConfigError
from kb.index import load_model
from kb.statements import comparable


@dataclass(frozen=True)
class Question:
    question: str
    expect: tuple[tuple[str, str], ...]  # (source_id glob, section_ref glob); any match is a hit
    tags: tuple[str, ...] = ()


def load(path: Path) -> list[Question]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: expected a non-empty list of questions")
    questions, errors = [], []
    for number, entry in enumerate(raw, start=1):
        try:
            expect = tuple((str(e["source"]), str(e["section"])) for e in entry["expect"])
            if not expect or not str(entry["question"]).strip():
                raise ValueError
            questions.append(Question(str(entry["question"]), expect, tuple(entry.get("tags") or ())))
        except (KeyError, TypeError, ValueError):
            errors.append(f"{path}: entry {number}: needs question and expect: [{{source, section}}, ...]")
    if errors:
        raise ConfigError("\n".join(errors))
    return questions


def is_hit(result: dict[str, object], expect: tuple[tuple[str, str], ...]) -> bool:
    """True when the result, or an identical section listed in also_in, is one of the expected sections."""
    places = [(str(result["source_id"]), str(result["section_ref"]))]
    places += [tuple(p.split(" ", 1)) for p in result.get("also_in", [])]  # type: ignore[attr-defined, misc]
    return any(_matches(source_id, ref, expect) for source_id, ref in places)


def _matches(source_id: str, ref: str, expect: tuple[tuple[str, str], ...]) -> bool:
    for source, section in expect:
        if fnmatch.fnmatchcase(source_id, source) and (
            fnmatch.fnmatchcase(ref, section) or ref.startswith(f"{section} (part ")
        ):
            return True
    return False


def bad_quotes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT s.id, s.verbatim_quote, c.text FROM statements s JOIN chunks c ON c.id = s.chunk_id"
    ).fetchall()
    return [rid for rid, quote, text in rows if comparable(quote) not in comparable(text)]


def run(db_path: Path, golden: Path, k: int, minimum: float) -> int:
    try:
        questions = load(golden)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    searcher = search.Searcher(lambda: load_model(offline=True))
    hits = 0
    try:
        for q in questions:
            results = search.search(conn, searcher, q.question, list(q.tags) or None, limit=k)["results"]
            rank = next((n for n, r in enumerate(results, start=1) if is_hit(r, q.expect)), None)  # type: ignore[arg-type]
            hits += rank is not None
            top = ", ".join(f"{r['source_id']} {r['section_ref']}" for r in results[:3])  # type: ignore[index]
            print(
                f"{'hit ' + str(rank) if rank else 'MISS ':<6} {q.question}" + ("" if rank else f"\n       got: {top}")
            )
        broken = bad_quotes(conn)
    finally:
        conn.close()
    rate = hits / len(questions)
    print(
        f"top-{k} hit rate {rate:.0%} ({hits}/{len(questions)}), target {minimum:.0%}; {len(broken)} quotes not found"
    )
    for rid in broken[:10]:
        print(f"  quote not in its section: {rid}")
    return 0 if rate >= minimum and not broken else 1
