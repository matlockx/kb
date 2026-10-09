"""Second-opinion review of stored statements against their verbatim section text.

Usage: uv run python scripts/judge_statements.py [--db data/kb.db] [--per-source 10] [--source ID ...]
       > judge.jsonl

Samples statements per source (deterministic: lowest sha256 of the statement id), sends each sample with
the surrounding section text to the model through the same agent CLI call (`KB_PI_COMMAND`) the extractor
uses, and asks for a verdict per statement: modality, summary faithfulness, topics and applies_to. Writes one
JSON line per source with the verdicts. The model is a reviewer, not the source of truth: every flagged record
still needs a human look at the section.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kb import domain, statements

WINDOW = 1_500  # characters of section text kept on each side of the quote
JUDGE_PROMPT = """You review statements extracted from a document. For each numbered record you get the section
text around its verbatim quote and the extracted fields. Judge each field against the text only.

Return one JSON object and nothing else: {"verdicts": [{"n": 1, "modality_ok": true, "summary_ok": true,
"topics_ok": true, "applies_to_ok": true, "is_statement": true, "note": ""}]}

- modality_ok: the modality id matches the force the text states (ids and their meaning are listed below).
- summary_ok: the summary states what the quote says, with its conditions, numbers and exceptions, and adds
  nothing the text does not say. False for a wrong actor, a dropped condition or a wrong number.
- topics_ok: the topic ids fit the statement (ids and their meaning are listed below).
- applies_to_ok: the tags fit the statement given the document's tags.
- is_statement: the quote is the kind of statement the modalities below describe, not a definition, a heading
  or navigation and boilerplate text.
- note: empty when every field is fine; otherwise one short sentence naming the problem.
"""


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _json_list(value: str) -> list[object]:
    """A JSON array as stored by kb (applies_to, tags); malformed values come back as the raw string."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    return parsed if isinstance(parsed, list) else [value]


def sample(conn: sqlite3.Connection, source_id: str, per_source: int) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT s.id, s.verbatim_quote, s.summary, s.modality, s.applies_to, c.section_ref, c.text, "
        "(SELECT group_concat(topic_id) FROM statement_topics t WHERE t.statement_id = s.id) "
        "FROM statements s JOIN chunks c ON c.id = s.chunk_id WHERE c.version_id = ("
        "SELECT id FROM document_versions WHERE document_id = ? ORDER BY last_checked_at DESC, fetched_at DESC "
        "LIMIT 1)",
        (source_id,),
    ).fetchall()
    rows.sort(key=lambda r: hashlib.sha256(r[0].encode()).hexdigest())
    picked = []
    for sid, quote, summary, modality, applies, ref, text, topics in rows[:per_source]:
        flat = statements.normalise(text)
        at = flat.find(statements.normalise(quote))
        start = max(0, at - WINDOW) if at >= 0 else 0
        excerpt = flat[start : start + 2 * WINDOW + len(quote)] if at >= 0 else flat[: 2 * WINDOW]
        picked.append({
            "id": sid, "section": ref, "text": excerpt, "verbatim_quote": quote, "summary": summary,
            "modality": modality, "topics": (topics or "").split(","), "applies_to": _json_list(applies),
        })  # fmt: skip
    return picked


def judge(system: str, source: dict[str, object], picked: list[dict[str, object]], model: str) -> list[object]:
    parts = [f"Document: {source['title']} ({source['publisher']}), tags: {source['tags']}\n"]
    for n, p in enumerate(picked, start=1):
        fields = {k: p[k] for k in ("verbatim_quote", "summary", "modality", "topics", "applies_to")}
        parts.append(f"## Record {n} (section {p['section']})\n<text>\n{p['text']}\n</text>\n{json.dumps(fields)}\n")
    raw = statements.call_pi(system, "\n".join(parts), model).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)["verdicts"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise statements.ExtractionError(f"judge output is not a verdict object: {raw[:120]!r}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Second-opinion review of stored statements.")
    parser.add_argument("--db", type=Path, default=Path("data/kb.db"))
    parser.add_argument("--domain", type=Path, default=Path("domain.yaml"))
    parser.add_argument("--per-source", type=int, default=10)
    parser.add_argument("--source", action="append", help="review only this source (repeatable)")
    parser.add_argument("--model", default=statements.DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    kb_domain = domain.load(args.domain)
    system = (
        JUDGE_PROMPT
        + "\nModalities:\n"
        + "".join(f"- {m.id}: {m.description}\n" for m in kb_domain.modalities)
        + "\nTopics:\n"
        + "".join(f"- {t.id}: {t.description}\n" for t in kb_domain.topics)
    )
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, check_same_thread=False)
    docs = conn.execute("SELECT id, title, publisher, tags FROM documents ORDER BY id").fetchall()
    jobs = []
    for doc_id, title, publisher, tags in docs:
        if args.source and doc_id not in args.source:
            continue
        picked = sample(conn, doc_id, args.per_source)
        if picked:
            jobs.append((doc_id, {"title": title, "publisher": publisher, "tags": _json_list(tags)}, picked))

    def run(job: tuple[str, dict[str, object], list[dict[str, object]]]) -> tuple[str, list[object] | str, list]:
        doc_id, source, picked = job
        try:
            return doc_id, judge(system, source, picked, args.model), picked
        except (statements.ExtractionError, OSError) as exc:
            return doc_id, f"judge failed: {exc}", picked

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for doc_id, verdicts, picked in pool.map(run, jobs):
            if isinstance(verdicts, str):
                out(json.dumps({"source": doc_id, "error": verdicts}))
                continue
            by_n = {v.get("n"): v for v in verdicts if isinstance(v, dict)}
            records = [{"id": p["id"], "section": p["section"], **by_n.get(n, {})} for n, p in enumerate(picked, 1)]
            out(json.dumps({"source": doc_id, "records": records}, ensure_ascii=False))


if __name__ == "__main__":
    main()
