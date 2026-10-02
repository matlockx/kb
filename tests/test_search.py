import dataclasses
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest
import yaml

from kb import db, index, parse, search, sources
from kb.chunk import Section
from kb.domain import Domain, Modality, Topic
from kb.sources import Source
from kb.statements import Statement, _store, sync_topics

DIM = 64
DOMAIN = Domain(
    name="Test",
    instructions="Test domain.",
    doc_types=("act", "regulation"),
    tags=("food", "toys", "cosmetics"),
    modalities=(Modality("must", "required"), Modality("should", "advised")),
    topics=(Topic("recalls", "Recalls", "d"), Topic("marketing", "Marketing", "d")),
)


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    """Bag of hashed words: texts sharing words are close, which is all ranking tests need."""
    out = np.zeros((len(texts), DIM), dtype="<f4")
    for row, text in enumerate(texts):
        for word in re.findall(r"\w+", text.lower()):
            out[row, int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIM] += 1
        out[row] /= max(np.linalg.norm(out[row]), 1e-9)
    return out


def source(id_: str, language: str = "en", **extra: object) -> Source:
    fields: dict[str, object] = {
        "id": id_, "publisher": "A", "title": f"Title {id_}", "url": f"https://example.org/{id_}",
        "language": language, "doc_type": "regulation", "tags": ("food", "toys"),
    }  # fmt: skip
    return Source(**{**fields, **extra})  # type: ignore[arg-type]


GB = source("gb-code")
GB_COPY = source("gb-code-retail")
SE = source("se-lag", "sv", doc_type="act", tags=("cosmetics",))


def stmt(quote: str, summary: str, modality: str = "must", tags: tuple[str, ...] = ("food",)) -> Statement:
    return Statement(quote, summary, modality, ("recalls",), tags)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "kb.db")
    sources.sync(conn, [GB, GB_COPY, SE])
    sync_topics(conn, DOMAIN.topics)
    for doc in ("gb-code", "gb-code-retail", "se-lag"):
        with conn:
            conn.execute(
                "INSERT INTO document_versions (id, document_id, sha256, raw_path, fetched_at, last_checked_at) "
                "VALUES (?, ?, ?, 'x', '2026-09-24T00:00:00Z', '2026-09-24T00:00:00Z')",
                (f"{doc}@v1", doc, hashlib.sha256(doc.encode()).hexdigest()),
            )
    gb_text = "Producers must run a post-market recall service for at least six months."
    parse.store(conn, "gb-code@v1", [
        Section("code 3.5.3", ("Product safety",), gb_text),
        Section("code 5.1.1 (part 1)", (), "Marketing must be truthful."),
        Section("code 5.1.1 (part 2)", (), "Promotional offers must not target children."),
    ])  # fmt: skip
    parse.store(conn, "gb-code-retail@v1", [Section("code 3.5.3", (), gb_text)])
    parse.store(conn, "se-lag@v1", [Section("14 kap. 1 §", (), "En producent ska erbjuda återkallelse.")])
    six_months = stmt(gb_text, "Producers must run post-market recalls for at least six months.")
    _store(conn, "gb-code@v1#0000", [six_months], "m", "p", "t")
    _store(conn, "gb-code-retail@v1#0000", [six_months], "m", "p", "t")
    swedish = stmt("En producent ska erbjuda återkallelse.", "Offer recalls.", tags=("cosmetics",))
    _store(conn, "se-lag@v1#0000", [swedish], "m", "p", "t")
    index.build_fts(conn)
    index.sync_vectors(conn, "chunk", index.chunk_texts(conn), index.EMBED_MODEL, fake_embed)
    index.sync_vectors(conn, "statement", index.statement_texts(conn), index.EMBED_MODEL, fake_embed)
    yield conn
    conn.close()


@pytest.fixture
def searcher() -> search.Searcher:
    return search.Searcher(lambda: fake_embed)


def refs(result: dict[str, object]) -> list[tuple[str, str]]:
    return [(r["source_id"], r["section_ref"]) for r in result["results"]]  # type: ignore[index, attr-defined]


def test_search_ranks_matching_sections_with_their_statements(
    conn: sqlite3.Connection, searcher: search.Searcher
) -> None:
    result = search.search(conn, searcher, "post-market recall six months", limit=3)
    assert refs(result)[0] in {("gb-code", "code 3.5.3"), ("gb-code-retail", "code 3.5.3")}
    first = result["results"][0]  # type: ignore[index]
    assert first["statements"][0]["summary"] == "Producers must run post-market recalls for at least six months."
    assert first["version"] and first["url"].startswith("https://")
    assert "note" not in result


def test_search_shows_identical_sections_once(conn: sqlite3.Connection, searcher: search.Searcher) -> None:
    result = search.search(conn, searcher, "post-market recall six months", tags=["food"])
    [section] = [r for r in result["results"] if r["section_ref"] == "code 3.5.3"]  # type: ignore[index, attr-defined]
    assert len(section["also_in"]) == 1


def test_search_filters(conn: sqlite3.Connection, searcher: search.Searcher) -> None:
    only_cosmetics = search.search(conn, searcher, "post-market recall", tags=["cosmetics"])
    assert {s for s, _ in refs(only_cosmetics)} == {"se-lag"}
    by_topic = search.search(conn, searcher, "marketing promotional", topics=["recalls"])
    assert all(ref != "code 5.1.1 (part 1)" for _, ref in refs(by_topic))


@pytest.mark.parametrize("query", ['" OR 1 NEAR(', "AND OR NOT *", 'post-market"; DROP TABLE chunks; --'])
def test_search_escapes_fts_syntax(conn: sqlite3.Connection, searcher: search.Searcher, query: str) -> None:
    search.search(conn, searcher, query)
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 5


def test_search_errors_and_notes(conn: sqlite3.Connection, searcher: search.Searcher) -> None:
    with pytest.raises(search.QueryError, match="unknown topics"):
        search.search(conn, searcher, "x", topics=["nope"])
    with pytest.raises(search.QueryError, match="empty"):
        search.search(conn, searcher, "  ")
    assert "do not fill the gap" in search.search(conn, searcher, "x", tags=["textiles"])["note"]  # type: ignore[operator]
    conn.execute("DELETE FROM vectors")
    assert "keyword-only" in search.search(conn, searcher, "post-market")["note"]  # type: ignore[operator]
    conn.execute("DROP TABLE chunks_fts")
    with pytest.raises(search.QueryError, match="run `kb index`"):
        search.search(conn, searcher, "post-market")


def test_get_section_joins_parts_and_suggests_refs(conn: sqlite3.Connection) -> None:
    section = search.get_section(conn, "gb-code", "code 5.1.1")
    assert section["text"] == "Marketing must be truthful.\nPromotional offers must not target children."
    with pytest.raises(search.QueryError, match=r"similar refs: .*code 5\.1\.1 \(part 1\)"):
        search.get_section(conn, "gb-code", "code 5.1.9")
    with pytest.raises(search.QueryError, match=r"'Annex A' in gb-code; similar refs: code 3\.5\.3"):
        search.get_section(conn, "gb-code", "Annex A")
    with pytest.raises(search.QueryError, match="unknown source_id 'gb-nope'"):
        search.get_section(conn, "gb-nope", "x")


def test_topic_dedupes_filters_and_notes(conn: sqlite3.Connection) -> None:
    result = search.topic(conn, DOMAIN, "recalls")
    assert result["statements_total"] == 2
    se, gb = result["statements"]  # type: ignore[misc]
    assert se["source_id"] == "se-lag"  # "act" comes before "regulation" in the domain
    assert (gb["source_id"], gb["also_in"]) == ("gb-code", ["gb-code-retail code 3.5.3"])
    cosmetics = search.topic(conn, DOMAIN, "recalls", tags=["cosmetics"])["statements"]
    assert [s["source_id"] for s in cosmetics] == ["se-lag"]  # type: ignore[index]
    assert "not proof that none exists" in search.topic(conn, DOMAIN, "marketing")["note"]  # type: ignore[operator]
    with pytest.raises(search.QueryError, match="unknown topics"):
        search.topic(conn, DOMAIN, "nope")


def test_topic_lists_strongest_modality_first(conn: sqlite3.Connection) -> None:
    text = "Producers should notify customers. Producers must withdraw unsafe products."
    with conn:  # a second section in the retail extract; parse.store refuses once statements cite a version
        conn.execute(
            "INSERT INTO chunks (id, version_id, ord, section_ref, heading_path, text, sha256) "
            "VALUES ('gb-code-retail@v1#0001', 'gb-code-retail@v1', 1, 'code 3.5.1', '[]', ?, 'x')",
            (text,),
        )
    _store(conn, "gb-code-retail@v1#0001", [
        stmt("Producers should notify customers.", "Notify customers.", "should"),
        stmt("Producers must withdraw unsafe products.", "Withdraw unsafe products."),
    ], "m", "p", "t")  # fmt: skip
    found = search.topic(conn, DOMAIN, "recalls")["statements"]
    assert [s["modality"] for s in found] == ["must", "must", "must", "should"]  # type: ignore[index]
    reversed_order = dataclasses.replace(DOMAIN, modalities=DOMAIN.modalities[::-1])
    assert search.topic(conn, reversed_order, "recalls")["statements"][0]["modality"] == "should"  # type: ignore[index]


def test_sources_lists_versions_and_topics(conn: sqlite3.Connection) -> None:
    listed = search.sources(conn)
    assert [s["source_id"] for s in listed["sources"]] == ["gb-code", "gb-code-retail", "se-lag"]  # type: ignore[attr-defined]
    se = listed["sources"][2]  # type: ignore[index]
    assert (se["statements"], se["sections"], se["tags"]) == (1, 1, ["cosmetics"])
    assert listed["topics"] == [
        {"id": "marketing", "label": "Marketing"},
        {"id": "recalls", "label": "Recalls"},
    ]


def test_searcher_reloads_vectors_after_reindex(conn: sqlite3.Connection, searcher: search.Searcher) -> None:
    ids, before = searcher.vectors(conn, "chunk")
    first = ids[0]
    texts = index.chunk_texts(conn)
    index.sync_vectors(conn, "chunk", {**texts, first: "completely different words"}, index.EMBED_MODEL, fake_embed)
    ids_after, after = searcher.vectors(conn, "chunk")
    assert len(ids_after) == len(ids)
    assert not np.array_equal(before[ids.index(first)], after[ids_after.index(first)])


def test_sync_vectors_is_incremental(conn: sqlite3.Connection) -> None:
    texts = index.chunk_texts(conn)
    assert index.sync_vectors(conn, "chunk", texts, index.EMBED_MODEL, fake_embed) == (0, 0)
    first = next(iter(texts))
    changed = {**texts, first: texts[first] + " amended"}
    del changed[list(texts)[-1]]
    assert index.sync_vectors(conn, "chunk", changed, index.EMBED_MODEL, fake_embed) == (1, 1)


@pytest.mark.usefixtures("conn")
def test_mcp_server_over_stdio(tmp_path: Path) -> None:
    """Speaks the protocol version the pi bridge sends."""
    domain_file = tmp_path / "domain.yaml"
    domain_file.write_text(
        yaml.safe_dump({
            "name": DOMAIN.name, "instructions": DOMAIN.instructions, "doc_types": list(DOMAIN.doc_types),
            "tags": list(DOMAIN.tags), "modalities": [dataclasses.asdict(m) for m in DOMAIN.modalities],
            "topics": [dataclasses.asdict(t) for t in DOMAIN.topics],
        }),
        encoding="utf-8",
    )  # fmt: skip
    messages = [
        {"id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pi", "version": "1"}}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list", "params": {}},
        {"id": 3, "method": "tools/call", "params": {"name": "kb_sources", "arguments": {}}},
        {"id": 4, "method": "tools/call",
         "params": {"name": "kb_get", "arguments": {"source_id": "x", "section_ref": "1"}}},
        {"id": 5, "method": "tools/call", "params": {"name": "kb_topic", "arguments": {"topic": "recalls"}}},
    ]  # fmt: skip
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", "import sys; from kb.cli import main; sys.exit(main(sys.argv[1:]))",
         "--db", str(tmp_path / "kb.db"), "--domain", str(domain_file), "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None
    replies: dict[int, dict[str, object]] = {}
    try:  # stdin stays open until every reply is in: the server drops in-flight calls at EOF
        for m in messages:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", **m}) + "\n")
            proc.stdin.flush()
            if "id" in m:
                while m["id"] not in replies:
                    reply = json.loads(proc.stdout.readline())
                    if "id" in reply:
                        replies[reply["id"]] = reply
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert "Test: Test domain." in replies[1]["result"]["instructions"]  # type: ignore[index]
    tools = replies[2]["result"]["tools"]  # type: ignore[index]
    assert {t["name"] for t in tools} == {"kb_search", "kb_get", "kb_topic", "kb_sources"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    listed = json.loads(replies[3]["result"]["content"][0]["text"])  # type: ignore[index]
    assert [s["source_id"] for s in listed["sources"]] == ["gb-code", "gb-code-retail", "se-lag"]
    assert "unknown source_id 'x'" in json.loads(replies[4]["result"]["content"][0]["text"])["error"]  # type: ignore[index]
    assert json.loads(replies[5]["result"]["content"][0]["text"])["statements_total"] == 2  # type: ignore[index]
