"""Compare two kb databases built from the same sources.yaml.

Usage: uv run python scripts/compare_kb.py data/kb.db /tmp/rerun/kb-rerun.db [--sections 40]

Compares the current version of each source in the base and the rerun database:
1. fetch: sha256 of the downloaded content;
2. parse: chunk count and section refs;
3. extract: for sections with identical text in both runs and extracted in the rerun, statement counts,
   modality and topic distributions, exact overlap of normalised verbatim quotes, and summary similarity
   (cosine of the stored bge-m3 statement vectors, best match per statement).
LLM extraction is not deterministic, so the report gives agreement rates and lists the sections with the
lowest agreement for manual review. Search quality is compared with `kb eval` on each database.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from kb.index import EMBED_MODEL, from_blob
from kb.statements import normalise

CURRENT = (
    "SELECT d.id, v.id, v.sha256 FROM documents d JOIN document_versions v ON v.document_id = d.id "
    "WHERE v.id = (SELECT w.id FROM document_versions w WHERE w.document_id = d.id "
    "ORDER BY w.last_checked_at DESC, w.fetched_at DESC LIMIT 1)"
)


def out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def current(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    # CURRENT is a constant query without interpolation.
    return {doc: (version, sha) for doc, version, sha in conn.execute(CURRENT)}


def chunks(conn: sqlite3.Connection, version_id: str) -> list[tuple[str, str, str]]:
    return conn.execute(
        "SELECT id, section_ref, sha256 FROM chunks WHERE version_id = ? ORDER BY ord", (version_id,)
    ).fetchall()


def statements(conn: sqlite3.Connection, chunk_id: str) -> list[tuple[str, str, str, frozenset[str]]]:
    rows = conn.execute(
        "SELECT r.id, r.verbatim_quote, r.modality, group_concat(t.topic_id) FROM statements r "
        "LEFT JOIN statement_topics t ON t.statement_id = r.id WHERE r.chunk_id = ? GROUP BY r.id",
        (chunk_id,),
    ).fetchall()
    return [(rid, normalise(q), m, frozenset((topics or "").split(","))) for rid, q, m, topics in rows]


def vectors(conn: sqlite3.Connection, ids: list[str]) -> np.ndarray | None:
    found = []
    for rid in ids:
        row = conn.execute(
            "SELECT vector FROM vectors WHERE kind = 'statement' AND item_id = ? AND model = ?", (rid, EMBED_MODEL)
        ).fetchone()
        if row is None:
            return None
        found.append(from_blob(row[0]))
    return np.stack(found) if found else None


def extracted_in(conn: sqlite3.Connection) -> set[str]:
    """Chunks the rerun sent to the model: those with statements or a cached output for their text.

    A cached empty output cannot be tied to a chunk id without rebuilding the prompt, so sections that
    yielded no statement in the rerun are counted only via the --extracted file written by the sampler.
    """
    return {row[0] for row in conn.execute("SELECT DISTINCT chunk_id FROM statements")}


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Mean best-match cosine in both directions; rows are L2-normalised and both matrices non-empty."""
    if a.size == 0 or b.size == 0 or a.shape[1] != b.shape[1]:
        return 0.0
    try:
        sims = a @ b.T
        return float((sims.max(axis=1).mean() + sims.max(axis=0).mean()) / 2)
    except ValueError:  # malformed vector blob; treat as no agreement rather than abort the report
        return 0.0


def jaccard(a: set[str], b: set[str]) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def compare_fetch_parse(base: sqlite3.Connection, rerun: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """Print fetch and parse differences; return source -> (base version, rerun version) with equal chunks."""
    b_cur, r_cur = current(base), current(rerun)
    same_sha = same_parse = 0
    pairs = {}
    out("## Fetch and parse")
    out()
    out("| source | fetch | base chunks | rerun chunks | refs only in base | refs only in rerun |")
    out("|---|---|---|---|---|---|")
    for doc in sorted(b_cur.keys() | r_cur.keys()):
        if doc not in b_cur or doc not in r_cur:
            out(f"| {doc} | missing in {'rerun' if doc in b_cur else 'base'} | | | | |")
            continue
        (bv, bsha), (rv, rsha) = b_cur[doc], r_cur[doc]
        bc, rc = chunks(base, bv), chunks(rerun, rv)
        b_refs, r_refs = Counter(c[1] for c in bc), Counter(c[1] for c in rc)
        same_sha += bsha == rsha
        if bsha == rsha and [c[1:] for c in bc] == [c[1:] for c in rc]:
            same_parse += 1
            pairs[doc] = (bv, rv)
            continue
        only_b = ", ".join(sorted((b_refs - r_refs).elements())[:6])
        only_r = ", ".join(sorted((r_refs - b_refs).elements())[:6])
        fetch = "same" if bsha == rsha else "changed"
        out(f"| {doc} | {fetch} | {len(bc)} | {len(rc)} | {only_b} | {only_r} |")
        if bsha == rsha:
            pairs[doc] = (bv, rv)
    out()
    out(f"{same_sha}/{len(b_cur)} sources fetched identical content; {same_parse} parsed to identical chunks.")
    out()
    return pairs


def compare_extract(
    base: sqlite3.Connection,
    rerun: sqlite3.Connection,
    pairs: dict[str, tuple[str, str]],
    extracted: set[str],
    worst: int,
) -> None:
    rows = []
    modality_b: Counter[str] = Counter()
    modality_r: Counter[str] = Counter()
    for doc, (bv, rv) in sorted(pairs.items()):
        b_by_key = {(ref, sha): cid for cid, ref, sha in chunks(base, bv)}
        for rcid, ref, sha in chunks(rerun, rv):
            bcid = b_by_key.get((ref, sha))
            if bcid is None or rcid not in extracted:
                continue
            breq, rreq = statements(base, bcid), statements(rerun, rcid)
            modality_b.update(m for _, _, m, _ in breq)
            modality_r.update(m for _, _, m, _ in rreq)
            quotes = jaccard({q for _, q, _, _ in breq}, {q for _, q, _, _ in rreq})
            topics = jaccard(set().union(*(t for *_, t in breq)), set().union(*(t for *_, t in rreq)))
            modal = 1 - sum((Counter(m for _, _, m, _ in breq) - Counter(m for _, _, m, _ in rreq)).values()) / max(
                len(breq), 1
            )
            bvec, rvec = vectors(base, [r[0] for r in breq]), vectors(rerun, [r[0] for r in rreq])
            # Both sides empty agree; a side without vectors does not.
            empty = 1.0 if not breq and not rreq else 0.0
            summary = empty if bvec is None or rvec is None else similarity(bvec, rvec)
            rows.append((doc, ref, len(breq), len(rreq), quotes, topics, modal, summary))
    out("## Extraction")
    out()
    if not rows:
        out("No section was extracted in both runs.")
        return
    count_equal = sum(b == r for _, _, b, r, *_ in rows)
    count_close = sum(abs(b - r) <= max(1, round(0.25 * max(b, r))) for _, _, b, r, *_ in rows)
    out(f"Sections compared: {len(rows)} (identical text, extracted in both runs).")
    out()
    out("| measure | value |")
    out("|---|---|")
    out(f"| statements, base / rerun | {sum(r[2] for r in rows)} / {sum(r[3] for r in rows)} |")
    out(f"| sections with equal statement count | {count_equal / len(rows):.0%} |")
    out(f"| sections with count within 25% (or 1) | {count_close / len(rows):.0%} |")
    out(f"| verbatim quote overlap (Jaccard), mean / median | {statistics.mean(r[4] for r in rows):.2f} / "
        f"{statistics.median(r[4] for r in rows):.2f} |")  # fmt: skip
    out(f"| topic set overlap (Jaccard), mean | {statistics.mean(r[5] for r in rows):.2f} |")
    out(f"| modality agreement, mean | {statistics.mean(r[6] for r in rows):.2f} |")
    out(f"| summary similarity (bge-m3 cosine, best match), mean | {statistics.mean(r[7] for r in rows):.2f} |")
    out(f"| modality distribution, base | {json.dumps(dict(modality_b.most_common()))} |")
    out(f"| modality distribution, rerun | {json.dumps(dict(modality_r.most_common()))} |")
    out()
    per_source: dict[str, list[float]] = {}
    for doc, *_, summary in rows:
        per_source.setdefault(doc, []).append(summary)
    out("### Lowest summary agreement per source (at least 3 sections)")
    out()
    out("| source | sections | mean summary similarity |")
    out("|---|---|---|")
    ranked = sorted((statistics.mean(v), doc, len(v)) for doc, v in per_source.items() if len(v) >= 3)
    for mean, doc, n in ranked[:15]:
        out(f"| {doc} | {n} | {mean:.2f} |")
    out()
    out(f"### {worst} sections with the lowest agreement")
    out()
    out("| source | section | base | rerun | quote overlap | topic overlap | summary similarity |")
    out("|---|---|---|---|---|---|---|")
    for doc, ref, b, r, quotes, topics, _, summary in sorted(rows, key=lambda x: (x[7], x[4]))[:worst]:
        out(f"| {doc} | {ref} | {b} | {r} | {quotes:.2f} | {topics:.2f} | {summary:.2f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two kb databases built from the same sources.yaml.")
    parser.add_argument("base", type=Path)
    parser.add_argument("rerun", type=Path)
    parser.add_argument("--extracted", type=Path, help="file of rerun chunk ids sent to the model, one per line")
    parser.add_argument("--sections", type=int, default=40, help="low-agreement sections to list")
    args = parser.parse_args()
    base = sqlite3.connect(f"file:{args.base}?mode=ro", uri=True)
    rerun = sqlite3.connect(f"file:{args.rerun}?mode=ro", uri=True)
    extracted = extracted_in(rerun)
    if args.extracted:
        extracted |= set(args.extracted.read_text(encoding="utf-8").split())
    pairs = compare_fetch_parse(base, rerun)
    compare_extract(base, rerun, pairs, extracted, args.sections)


if __name__ == "__main__":
    main()
