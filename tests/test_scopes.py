import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from kb import db, domain, evaluate, index, parse, scopes, search, sources, statements
from kb.chunk import Section
from kb.domain import Availability, ConfigError, Domain, Modality, Topic
from kb.sources import Source
from tests.test_search import fake_embed

DOMAIN = Domain(
    name="Rules",
    instructions="Rules per market.",
    doc_types=("act", "guidance", "case_law"),
    tags=("casino", "lottery", "other"),
    modalities=(Modality("must", "required"), Modality("may", "allowed")),
    topics=(Topic("limits", "Limits", "d"), Topic("ads", "Ads", "d")),
    non_binding=frozenset({"case_law"}),
    doc_type_notes={"case_law": "how a court read the law"},
    scope_label="jurisdiction",
    availability=Availability(("casino", "lottery"), ("licensed", "prohibited"), "unknown"),
)


def source(id_: str, scope: str, language: str = "en", **extra: object) -> Source:
    fields: dict[str, object] = {
        "id": id_, "publisher": "P", "title": f"Title {id_}", "url": f"https://example.org/{id_}",
        "language": language, "doc_type": "act", "tags": ("casino", "lottery"), "scope": scope,
    }  # fmt: skip
    return Source(**{**fields, **extra})  # type: ignore[arg-type]


GB_ACT = source("gb-act", "GB")
GB_CASE = source("gb-case", "GB", doc_type="case_law")
SE_LAW = source("se-lag", "SE", "sv")
SE_EN = source("se-lag-en", "SE", translation_of="se-lag", extract_note="Read should as must.")
SOURCES = [GB_ACT, GB_CASE, SE_LAW, SE_EN]

REGISTRY = [
    {
        "id": "GB",
        "name": "Great Britain",
        "aliases": ["uk", "british"],
        "languages": ["en"],
        "details": {"regulator": "Commission", "eu": False},
        "availability": {
            "casino": {"status": "licensed", "source": "gb-act"},
            "lottery": {"status": "licensed", "source": "gb-act", "effective_from": "2999-01", "note": "later"},
        },
    },
    {
        "id": "SE",
        "name": "Sweden",
        "languages": ["sv"],
        "availability": {"casino": {"status": "unknown"}, "lottery": {"status": "prohibited", "source": "se-lag"}},
    },
]


def write(tmp_path: Path, entries: object) -> Path:
    path = tmp_path / "scopes.yaml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return path


def test_registry_loads_with_availability_in_domain_order(tmp_path: Path) -> None:
    gb, se = scopes.load(write(tmp_path, REGISTRY), DOMAIN, SOURCES)
    assert (gb.id, gb.aliases, gb.details) == ("GB", ("uk", "british"), {"regulator": "Commission", "eu": False})
    assert [(s.tag, s.status, s.source, s.effective_from) for s in gb.availability] == [
        ("casino", "licensed", "gb-act", None),
        ("lottery", "licensed", "gb-act", "2999-01"),
    ]
    assert [s.status for s in se.availability] == ["unknown", "prohibited"]


def test_yaml_dates_are_read_as_text(tmp_path: Path) -> None:
    path = tmp_path / "scopes.yaml"
    text = yaml.safe_dump(REGISTRY).replace("effective_from: 2999-01", "effective_from: 2999-01-01")
    path.write_text(text, encoding="utf-8")
    [gb, _] = scopes.load(path, DOMAIN, SOURCES)
    assert gb.availability[1].effective_from == "2999-01-01"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda e: e.update(colour="red"), "unknown field 'colour'"),
        (lambda e: e.update(id="G B"), "id must be letters and digits"),
        (lambda e: e.update(name=" "), "name must be a non-empty string"),
        (lambda e: e.update(aliases=[""]), "aliases must be a list"),
        (lambda e: e.update(languages=["eng"]), "languages must be a list of two-letter"),
        (lambda e: e.update(languages=["en", "en"]), "languages contains duplicates"),
        (lambda e: e.update(details={"x": [1]}), "details must be a mapping"),
        (lambda e: e.update(availability=[]), "availability must be a mapping"),
        (lambda e: e["availability"].pop("casino"), "availability is missing ['casino']"),
        (lambda e: e["availability"].update(other={"status": "unknown"}), "unknown tags ['other']"),
        (lambda e: e["availability"].update(casino="licensed"), "availability.casino: expected a mapping"),
        (lambda e: e["availability"]["casino"].update(status="open"), "status must be one of"),
        (lambda e: e["availability"]["casino"].pop("source"), "needs a source that backs it"),
        (lambda e: e["availability"]["casino"].update(note=""), "note must be a non-empty string"),
        (lambda e: e["availability"]["casino"].update(effective_from="2026-13"), "effective_from must be YYYY"),
        (lambda e: e["availability"]["casino"].update(since="2026"), "unknown field 'since'"),
    ],
)
def test_registry_reports_entry_problems(tmp_path: Path, change: object, problem: str) -> None:
    entries = json.loads(json.dumps(REGISTRY))
    change(entries[0])  # type: ignore[operator]
    with pytest.raises(ConfigError, match=problem.replace("[", r"\[").replace("]", r"\]").replace("(", r"\(")):
        scopes.load(write(tmp_path, entries), DOMAIN, SOURCES)


def test_registry_reports_file_and_cross_registry_problems(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="expected a non-empty list of jurisdiction entries"):
        scopes.load(write(tmp_path, {}), DOMAIN, SOURCES)
    with pytest.raises(ConfigError, match="entry 1: expected a mapping"):
        scopes.load(write(tmp_path, ["GB"]), DOMAIN, SOURCES)
    with pytest.raises(ConfigError, match="duplicate id 'gb'"):
        scopes.load(write(tmp_path, [REGISTRY[0], {**REGISTRY[0], "id": "gb"}, REGISTRY[1]]), DOMAIN, SOURCES)
    with pytest.raises(ConfigError, match="No such file"):
        scopes.load(tmp_path / "missing.yaml", DOMAIN, SOURCES)
    plain = Domain(**{**DOMAIN.__dict__, "availability": None})
    with pytest.raises(ConfigError, match=r"availability needs availability in domain\.yaml"):
        scopes.load(write(tmp_path, REGISTRY), plain, SOURCES)

    problems = scopes.check(
        scopes.load(write(tmp_path, REGISTRY), DOMAIN, SOURCES),
        [source("x", "FR"), source("gb-de", "GB", "de"), SE_LAW, source("gb-act", "SE", "sv")],
    )
    assert problems == [
        "source x: scope 'FR' is not in the registry",
        "source gb-de: language 'de' is not one of GB languages ['en']; a translation needs translation_of pointing "
        "at the original",
        "GB availability.casino: source 'gb-act' belongs to SE",
        "GB availability.lottery: source 'gb-act' belongs to SE",
    ]
    assert scopes.check(scopes.load(write(tmp_path, REGISTRY), DOMAIN, SOURCES), [GB_CASE, SE_LAW]) == [
        "GB availability.casino: source 'gb-act' is not in the source registry",
        "GB availability.lottery: source 'gb-act' is not in the source registry",
    ]


def test_domain_parses_doc_type_rules_scopes_and_availability() -> None:
    text = yaml.safe_dump(
        {
            "name": "Rules",
            "instructions": "x",
            "doc_types": [
                "act",
                {"id": "case_law", "binding": False, "note": "a  court's reading"},
                {"id": "memo", "note": "m"},
            ],
            "tags": ["casino", "lottery"],
            "modalities": [{"id": "must", "description": "d"}],
            "topics": [{"id": "limits", "label": "L", "description": "d"}],
            "scopes": {"label": "jurisdiction"},
            "availability": {"tags": ["casino"], "values": ["licensed"], "unknown": "unknown"},
        }
    )
    parsed = domain.parse(text, "d")
    assert parsed.doc_types == ("act", "case_law", "memo")
    assert parsed.non_binding == frozenset({"case_law"})
    assert parsed.doc_type_notes == {"case_law": "a court's reading", "memo": "m"}
    assert (parsed.scope_label, parsed.availability) == (
        "jurisdiction",
        Availability(("casino",), ("licensed",), "unknown"),
    )


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"doc_types": ["act", {"id": "case_law", "binding": False}]}, "binding: false needs a note"),
        ({"doc_types": [{"id": "act", "binding": "no"}]}, "binding must be true or false"),
        ({"doc_types": [{"id": "act", "note": ""}]}, "note must be a non-empty string"),
        ({"doc_types": [{"id": "act", "rank": 1}]}, "unknown fields \\['rank'\\]"),
        ({"doc_types": [{"id": "Act"}]}, "doc_types: 'Act' must be lowercase"),
        ({"scopes": {"label": ""}}, "scopes must be a mapping with exactly label"),
        (
            {"scopes": None, "availability": {"tags": ["casino"], "values": ["a"], "unknown": "u"}},
            "availability needs scopes",
        ),
        ({"availability": ["casino"]}, "availability must be a mapping with exactly"),
        (
            {"availability": {"tags": ["poker"], "values": ["a"], "unknown": "u"}},
            "availability.tags \\['poker'\\] are not in tags",
        ),
        (
            {"availability": {"tags": ["casino"], "values": ["a"], "unknown": "a"}},
            "must not be one of availability.values",
        ),
        ({"availability": {"tags": ["casino"], "values": ["a"], "unknown": "U U"}}, "availability.unknown must be one"),
        ({"availability": {"tags": [], "values": ["a"], "unknown": "u"}}, "availability.tags must be a non-empty list"),
    ],
)
def test_domain_reports_rule_problems(change: dict[str, object], problem: str) -> None:
    raw: dict[str, object] = {
        "name": "Rules",
        "instructions": "x",
        "doc_types": ["act"],
        "tags": ["casino"],
        "modalities": [{"id": "must", "description": "d"}],
        "topics": [{"id": "limits", "label": "L", "description": "d"}],
        "scopes": {"label": "jurisdiction"},
        **change,
    }
    raw = {k: v for k, v in raw.items() if v is not None}
    with pytest.raises(ConfigError, match=problem):
        domain.parse(yaml.safe_dump(raw), "d")


def registry_entry(**fields: object) -> dict[str, object]:
    return {
        "id": "a", "publisher": "P", "title": "T", "url": "https://example.org/a", "language": "en",
        "doc_type": "act", "tags": ["casino"], **fields,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("entries", "problem"),
    [
        ([registry_entry()], "scope must be the id of a jurisdiction in scopes.yaml"),
        ([registry_entry(scope="GB", extract_note=" ")], "extract_note must be a non-empty string"),
        ([registry_entry(scope="GB", translation_of="zz")], "translation_of 'zz' is not a source id"),
        ([registry_entry(scope="GB", translation_of="a")], "translation_of points at itself"),
        (
            [registry_entry(scope="GB"), registry_entry(id="b", scope="SE", translation_of="a")],
            "translation_of 'a' has the same language.*\n.*has another scope",
        ),
        (
            [
                registry_entry(scope="GB", language="de"),
                registry_entry(id="b", scope="GB", translation_of="a"),
                registry_entry(id="c", scope="GB", language="fr", translation_of="b"),
            ],
            "translation_of 'b' is a translation itself",
        ),
    ],
)
def test_sources_check_scope_and_translations(tmp_path: Path, entries: list[object], problem: str) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    with pytest.raises(ConfigError, match=problem):
        sources.load(path, DOMAIN)


def test_sources_reject_a_scope_without_scopes_in_the_domain(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump([registry_entry(scope="GB")]), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"scope needs scopes in domain\.yaml"):
        sources.load(path, Domain(**{**DOMAIN.__dict__, "scope_label": None, "availability": None}))


def test_connect_adds_columns_to_a_schema_1_database(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE documents (id TEXT PRIMARY KEY, publisher TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, "
        "language TEXT NOT NULL, doc_type TEXT NOT NULL, tags TEXT NOT NULL)"
    )
    old.execute("INSERT INTO documents VALUES ('a', 'P', 'T', 'u', 'en', 'act', '[]')")
    old.commit()
    old.close()
    db.upgrade(path)
    conn = sqlite3.connect(path)
    assert [r[1] for r in conn.execute("PRAGMA table_info(documents)")][-2:] == ["scope", "translation_of"]
    assert [r[1] for r in conn.execute("PRAGMA table_info(statements)")][-2:] == ["effective_from", "original_chunk_id"]
    assert conn.execute("SELECT id, scope FROM documents").fetchall() == [("a", None)]
    conn.close()
    path.chmod(0o444)  # a current database is never opened for writing, so a read-only copy serves
    try:
        db.upgrade(path)
    finally:
        path.chmod(0o644)


def record(quote: str, **extra: object) -> dict[str, object]:
    return {
        "verbatim_quote": quote, "summary": f"S: {quote}", "modality": "must", "topics": ["limits"],
        "applies_to": ["casino"], **extra,
    }  # fmt: skip


@pytest.fixture
def messages() -> list[str]:
    """Every extraction message the conn fixture sent to the model."""
    return []


@pytest.fixture
def conn(tmp_path: Path, messages: list[str]) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "kb.db")
    sources.sync(conn, SOURCES)
    statements.sync_topics(conn, DOMAIN.topics)
    scopes.sync(conn, scopes.load(write(tmp_path, REGISTRY), DOMAIN, SOURCES))
    texts = {
        "gb-act": [Section("s. 1", (), "Operators must cap deposits at 100 pounds.")],
        "gb-case": [Section("para. 3", (), "The court held that operators must cap deposits.")],
        "se-lag": [Section("3 kap. 1 §", (), "Spelbolag ska begränsa insättningar.")],
        "se-lag-en": [Section("Chapter 3 § 1", (), "Gaming companies must limit deposits from 2027.")],
    }
    for doc, sections in texts.items():
        with conn:
            conn.execute(
                "INSERT INTO document_versions (id, document_id, sha256, raw_path, fetched_at, last_checked_at) "
                "VALUES (?, ?, ?, 'x', '2026-09-24T00:00:00Z', '2026-09-24T00:00:00Z')",
                (f"{doc}@v1", doc, doc * 4),
            )
        parse.store(conn, f"{doc}@v1", sections)

    def call(_system: str, message: str, _model: str) -> str:
        messages.append(message)
        text = message.split("<section>\n", 1)[1].split("\n</section>", 1)[0]
        extra = {"effective_from": "2027"} if "2027" in text else {}
        return json.dumps({"statements": [record(text, **extra), record(text, effective_from="soon")]})

    reports = statements.extract_all(conn, SOURCES, DOMAIN, "system", call=call)
    assert all(len(r.rejected) == 1 and "effective_from 'soon'" in r.rejected[0] for r in reports)
    index.build_fts(conn)
    index.sync_vectors(conn, "chunk", index.chunk_texts(conn), index.EMBED_MODEL, fake_embed)
    index.sync_vectors(conn, "statement", index.statement_texts(conn), index.EMBED_MODEL, fake_embed)
    yield conn
    conn.close()


def test_extraction_message_names_scope_and_note_and_links_translations(
    conn: sqlite3.Connection, messages: list[str]
) -> None:
    assert any(m.startswith("Document: Title gb-act (P, GB)\n") for m in messages)  # calls run in a thread pool
    [translated] = [m for m in messages if "se-lag-en" in m]
    assert "Tags: casino, lottery\nRead should as must.\nSection: Chapter 3 § 1\n" in translated
    rows = conn.execute("SELECT id, effective_from, original_chunk_id FROM statements ORDER BY id").fetchall()
    assert rows == [
        ("gb-act@v1#0000/1", None, None),
        ("gb-case@v1#0000/1", None, None),
        ("se-lag-en@v1#0000/1", "2027", "se-lag@v1#0000"),
        ("se-lag@v1#0000/1", None, None),
    ]


def test_scopes_filter_and_inference(conn: sqlite3.Connection) -> None:
    searcher = search.Searcher(lambda: fake_embed)
    named = search.search(conn, searcher, DOMAIN, "uk deposits")
    assert named["scopes_inferred"] == ["GB"]
    assert {r["source_id"] for r in named["results"]} == {"gb-act", "gb-case"}  # type: ignore[index, union-attr]
    given = search.search(conn, searcher, DOMAIN, "uk deposits", scopes=["se"])
    assert "scopes_inferred" not in given
    assert {r["source_id"] for r in given["results"]} == {"se-lag", "se-lag-en"}  # type: ignore[index, union-attr]
    with pytest.raises(search.QueryError, match=r"unknown scopes \['FR'\]; use one of: GB, SE"):
        search.search(conn, searcher, DOMAIN, "deposits", scopes=["FR"])
    assert search._fts_query("deposits in the uk", frozenset({"uk"})) == '"deposits"*'


def test_scopes_on_a_knowledge_base_without_them(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "kb.db")
    with pytest.raises(search.QueryError, match="has no scopes"):
        search.resolve_scopes(conn, ["GB"])
    assert search.resolve_scopes(conn, None) is None
    conn.close()


def test_sections_and_statements_say_what_binds(conn: sqlite3.Connection) -> None:
    act = search.get_section(conn, DOMAIN, "gb-act", "s. 1")
    assert (act["scope"], act["binding"]) == ("GB", True)
    assert "note" not in act and "translation_of" not in act
    case = search.get_section(conn, DOMAIN, "gb-case", "para. 3")
    assert (case["binding"], case["note"]) == (False, "how a court read the law")
    translation = search.get_section(conn, DOMAIN, "se-lag-en", "Chapter 3 § 1")
    assert (translation["binding"], translation["translation_of"]) == (False, "se-lag")
    assert translation["note"] == search.TRANSLATION_NOTE
    [stated] = translation["statements"]  # type: ignore[misc]
    assert stated["effective_from"] == "2027"
    assert stated["original_section"] == {"source_id": "se-lag", "section_ref": "3 kap. 1 §"}

    plain = Domain(**{**DOMAIN.__dict__, "non_binding": frozenset(), "doc_type_notes": {}})
    assert "binding" not in search.get_section(conn, plain, "gb-act", "s. 1")  # nothing to tell apart


def test_topic_per_scope_with_availability(conn: sqlite3.Connection) -> None:
    result = search.topic(conn, DOMAIN, "limits", scopes=["gb", "SE", "FR"], today="2026-10-05")
    assert result["as_of"] == "2026-10-05"
    gb = result["scopes"]["GB"]  # type: ignore[index]
    assert gb["availability"] == [
        {"tag": "casino", "status": "licensed", "source_id": "gb-act"},
        {"tag": "lottery", "status": "licensed", "effective_from": "2999-01", "in_force": False, "source_id": "gb-act",
         "note": "later"},
    ]  # fmt: skip
    case_item = next(s for s in gb["statements"] if s["source_id"] == "gb-case")
    assert (case_item["binding"], case_item["note"]) == (False, "how a court read the law")
    se = result["scopes"]["SE"]  # type: ignore[index]
    assert [s["source_id"] for s in se["statements"]] == ["se-lag-en"]  # the original comes through its translation
    assert result["scopes"]["FR"] == {"error": "not in the knowledge base"}  # type: ignore[index]
    lottery = search.topic(conn, DOMAIN, "limits", scopes=["GB"], tags=["lottery"], today="2026-10-05")
    assert [a["tag"] for a in lottery["scopes"]["GB"]["availability"]] == ["lottery"]  # type: ignore[index]
    unscoped = search.topic(conn, DOMAIN, "limits")
    assert "scopes" not in unscoped and unscoped["statements_total"] == 3


def test_sources_list_scopes_with_details_and_availability(conn: sqlite3.Connection) -> None:
    listed = search.sources(conn, DOMAIN)
    assert listed["scopes"]["GB"]["regulator"] == "Commission"  # type: ignore[index]
    assert listed["scopes"]["GB"]["aliases"] == ["uk", "british"]  # type: ignore[index]
    assert "aliases" not in listed["scopes"]["SE"]  # type: ignore[index]
    assert listed["doc_type_notes"] == {"case_law": "how a court read the law"}
    by_id = {s["source_id"]: s for s in listed["sources"]}  # type: ignore[union-attr]
    assert (by_id["se-lag-en"]["binding"], by_id["se-lag-en"]["translation_of"]) == (False, "se-lag")
    assert "note" not in by_id["gb-case"]
    only = search.sources(conn, DOMAIN, "se")
    assert [s["source_id"] for s in only["sources"]] == ["se-lag", "se-lag-en"]  # type: ignore[union-attr]
    assert list(only["scopes"]) == ["SE"]  # type: ignore[arg-type]


def test_golden_questions_take_scopes(tmp_path: Path) -> None:
    path = tmp_path / "golden.yaml"
    path.write_text(
        yaml.safe_dump([{"question": "q", "scopes": ["GB"], "expect": [{"source": "gb-*", "section": "s. 1"}]}]),
        encoding="utf-8",
    )
    [question] = evaluate.load(path)
    assert question.scopes == ("GB",)


@pytest.mark.usefixtures("conn")
def test_golden_run_limits_questions_to_their_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(evaluate, "load_model", lambda **_: fake_embed)
    path = tmp_path / "golden.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {"question": "deposits", "scopes": ["SE"], "expect": [{"source": "se-*", "section": "*"}]},
                {"question": "uk deposits", "expect": [{"source": "gb-*", "section": "*"}]},
            ]
        ),
        encoding="utf-8",
    )
    assert evaluate.run(tmp_path / "kb.db", DOMAIN, path, 1, 1.0) == 0
    assert "top-1 hit rate 100% (2/2)" in capsys.readouterr().out


@pytest.mark.usefixtures("conn")
def test_server_describes_and_serves_scopes(tmp_path: Path) -> None:
    from kb import server

    built = server.build(tmp_path / "kb.db", DOMAIN, lambda: fake_embed)
    tools = {t.name: t for t in asyncio.run(built.list_tools())}
    assert "Filter by jurisdiction with scopes" in str(tools["kb_search"].description)
    assert "(licensed, prohibited or unknown per tag)" in str(tools["kb_topic"].description)
    assert "original_section" in str(tools["kb_search"].description)
    reply = asyncio.run(built.call_tool("kb_topic", {"topic": "limits", "scopes": ["SE"]}))
    text = reply.content[0].text  # type: ignore[union-attr]
    assert json.loads(text)["scopes"]["SE"]["statements"][0]["source_id"] == "se-lag-en"
    listed = json.loads(asyncio.run(built.call_tool("kb_sources", {"scope": "gb"})).content[0].text)  # type: ignore[union-attr]
    assert list(listed["scopes"]) == ["GB"]

    plain = Domain(**{**DOMAIN.__dict__, "scope_label": None, "availability": None, "non_binding": frozenset()})
    unscoped = {
        t.name: t for t in asyncio.run(server.build(tmp_path / "kb.db", plain, lambda: fake_embed).list_tools())
    }
    assert "has no scopes; leave scopes out" in str(unscoped["kb_search"].description)
