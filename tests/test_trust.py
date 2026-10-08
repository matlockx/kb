import sqlite3
import uuid
from pathlib import Path

import pytest
import yaml

from kb import db, domain, quality, review, search, trust
from kb.cli import main
from kb.domain import ConfigError, Domain, Modality, Publisher, Topic
from kb.sources import Source

EXAMPLES = Path(__file__).parents[1] / "examples" / "web-principles"
DOMAIN = Domain(
    name="Test",
    instructions="Test domain.",
    doc_types=("act", "guide"),
    tags=("food",),
    modalities=(Modality("must", "required"),),
    topics=(Topic("labelling", "Labelling", "labels"),),
    non_binding=frozenset({"guide"}),
    publishers=(
        Publisher("Parliament", ("example.org",), True),
        Publisher("Blog", ("blog.example.net",), False),
    ),
)
TEXT = "Operators must label allergens. Retailers should keep records."


def source(id_: str = "a", publisher: str = "Parliament", url: str = "https://example.org/a") -> Source:
    return Source(id_, publisher, "Title", url, "en", "act", ("food",))


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "kb.db")
    yield connection
    connection.close()


def add(
    conn: sqlite3.Connection,
    id_: str,
    publisher: str = "Parliament",
    url: str = "https://example.org/a",
    doc_type: str = "act",
    translation_of: str | None = None,
    checked: str = "2026-01-01T00:00:00Z",
) -> str:
    """A document with one version, one section holding TEXT and no statements; returns the version id."""
    version = f"{id_}@{checked}"
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO documents (id, publisher, title, url, language, doc_type, tags, translation_of) "
            "VALUES (?, ?, 'T', ?, 'en', ?, '[\"food\"]', ?)",
            (id_, publisher, url, doc_type, translation_of),
        )
        conn.execute(
            "INSERT INTO document_versions (id, document_id, sha256, raw_path, fetched_at, last_checked_at) "
            "VALUES (?, ?, ?, 'x', ?, ?)",
            (version, id_, version, checked, checked),
        )
        conn.execute(
            "INSERT INTO chunks (id, version_id, ord, section_ref, heading_path, text, sha256) "
            "VALUES (?, ?, 0, '1', '[]', ?, 'h')",
            (f"{version}#1", version, TEXT),
        )
    return version


def state(conn: sqlite3.Connection, id_: str) -> tuple[str, str]:
    found = trust.assess(conn, DOMAIN)[id_]
    return found.level, found.reason


@pytest.mark.parametrize(
    ("url", "declared"),
    [
        ("https://example.org/a", True),
        ("https://www.example.org/a", True),
        ("https://example.org:8443/a", True),
        ("https://example.org.evil.com/a", False),  # a look-alike that merely starts with the domain
        ("https://evilexample.org/a", False),  # a look-alike that merely ends with it
        ("https://example.org@evil.com/a", False),  # the host is what follows the @
        ("https://blog.example.net/a", False),  # a domain of another publisher
    ],
)
def test_a_url_must_be_on_a_domain_of_its_publisher(url: str, declared: bool) -> None:
    found, problem = trust.provenance(DOMAIN, "Parliament", url)
    assert (problem is None) is declared
    assert found is not None  # the publisher itself is declared in either case


def test_publishers_are_required_and_every_problem_is_listed() -> None:
    with pytest.raises(ConfigError, match="declares no publishers"):
        trust.check_publishers(Domain(**{**DOMAIN.__dict__, "publishers": ()}), [source()])
    trust.check_publishers(DOMAIN, [source(), source("b", "Blog", "https://blog.example.net/b")])
    with pytest.raises(ConfigError) as caught:
        trust.check_publishers(DOMAIN, [source("c", "Nobody"), source("d", url="https://evil.example/d"), source("e")])
    lines = str(caught.value).splitlines()
    assert len(lines) == 2
    assert "c: publisher 'Nobody' is not declared" in lines[0]
    assert "d: host 'evil.example' is not on a domain of 'Parliament' (example.org)" in lines[1]


def test_the_shipped_example_declares_its_publisher() -> None:
    defined = domain.load(EXAMPLES / "domain.yaml")
    trust.check_publishers(defined, [source("w3c", "W3C Technical Architecture Group", "https://www.w3.org/TR/x/")])


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"publishers": []}, "publishers must be a non-empty list"),
        ({"publishers": ["W3C"]}, "needs exactly domains, name, official"),
        ({"publishers": [{"name": "A", "domains": ["a.org"]}]}, "needs exactly domains, name, official"),
        ({"publishers": [{"name": " ", "domains": ["a.org"], "official": True}]}, "name must be a non-empty string"),
        ({"publishers": [{"name": "A", "domains": ["a.org"], "official": "yes"}]}, "official must be true or false"),
        ({"publishers": [{"name": "A", "domains": [], "official": True}]}, "domains must be a non-empty list"),
        ({"publishers": [{"name": "A", "domains": ["org"], "official": True}]}, "'org' is not a lowercase host name"),
        ({"publishers": [{"name": "A", "domains": ["A.org/x"], "official": True}]}, "'A.org/x' is not a lowercase"),
        (
            {"publishers": [{"name": "A", "domains": ["a.org"], "official": True}] * 2},
            "duplicate name 'A'",
        ),
    ],
)
def test_domain_rejects_malformed_publishers(change: dict[str, object], problem: str) -> None:
    raw = yaml.safe_load((EXAMPLES / "domain.yaml").read_text(encoding="utf-8")) | change
    with pytest.raises(ConfigError, match=problem):
        domain.parse(yaml.safe_dump(raw), "d")


def test_a_domain_without_publishers_still_loads_so_older_databases_keep_serving() -> None:
    raw = yaml.safe_load((EXAMPLES / "domain.yaml").read_text(encoding="utf-8"))
    del raw["publishers"]
    assert domain.parse(yaml.safe_dump(raw), "d").publishers == ()


@pytest.mark.parametrize(
    ("kind", "level", "why"),
    [
        ({}, "official", "Parliament is an official publisher on example.org and act is binding"),
        ({"doc_type": "guide"}, "secondary", "guide is not binding"),
        ({"publisher": "Blog", "url": "https://blog.example.net/a"}, "secondary", "Blog is not an official publisher"),
        ({"translation_of": "o"}, "secondary", "a translation"),
        ({"publisher": "Nobody"}, "unverified", "publisher 'Nobody' is not declared in domain.yaml publishers"),
        ({"url": "https://evil.example/a"}, "unverified", "host 'evil.example' is not on a domain of 'Parliament'"),
    ],
)
def test_trust_starts_from_the_publisher_and_the_doc_type(
    conn: sqlite3.Connection, kind: dict[str, str], level: str, why: str
) -> None:
    add(conn, "a", **kind)
    found_level, reason = state(conn, "a")
    assert found_level == level
    assert why in reason


def test_a_vetted_verdict_raises_one_level_and_a_disputed_one_overrides_it(conn: sqlite3.Connection) -> None:
    add(conn, "guide", doc_type="guide")  # secondary
    add(conn, "unknown", publisher="Nobody")  # unverified
    add(conn, "act")  # official
    for id_ in ("guide", "unknown", "act"):
        trust.record(conn, id_, "MJ", "vetted", "")
    assert [state(conn, i)[0] for i in ("guide", "unknown", "act")] == ["official", "secondary", "official"]
    assert "vetted by MJ" in state(conn, "guide")[1]

    trust.record(conn, "guide", "AB", "disputed", "The page is a mirror.")
    level, reason = state(conn, "guide")
    assert level == "disputed"
    assert "disputed by AB: The page is a mirror." in reason
    assert "otherwise secondary: guide is not binding" in reason  # what it would be without the dispute


def test_the_latest_verdict_of_a_reviewer_counts(conn: sqlite3.Connection) -> None:
    add(conn, "guide", doc_type="guide")
    trust.record(conn, "guide", "MJ", "disputed", "Looks wrong.")
    assert state(conn, "guide")[0] == "disputed"
    trust.record(conn, "guide", "MJ", "vetted", "Checked against the publisher's site.")
    assert state(conn, "guide")[0] == "official"


def test_a_new_version_of_a_source_starts_without_verdicts(conn: sqlite3.Connection) -> None:
    add(conn, "guide", doc_type="guide")
    trust.record(conn, "guide", "MJ", "vetted", "")
    assert state(conn, "guide")[0] == "official"
    add(conn, "guide", doc_type="guide", checked="2026-02-01T00:00:00Z")
    assert state(conn, "guide")[0] == "secondary"
    assert conn.execute("SELECT count(*) FROM reviews").fetchone() == (1,)  # kept as history


def test_a_verdict_needs_a_known_fetched_source_and_a_reason_to_dispute(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "INSERT INTO documents (id, publisher, title, url, language, doc_type, tags) "
            "VALUES ('bare', 'Parliament', 'T', 'https://example.org/b', 'en', 'act', '[]')"
        )
    with pytest.raises(trust.ReviewError, match="no fetched version"):
        trust.record(conn, "bare", "MJ", "vetted", "")
    with pytest.raises(trust.ReviewError, match="unknown source id: nope"):
        trust.record(conn, "nope", "MJ", "vetted", "")
    add(conn, "a")
    with pytest.raises(trust.ReviewError, match="needs a note"):
        trust.record(conn, "a", "MJ", "disputed", "  ")
    with pytest.raises(trust.ReviewError, match="verdict must be"):
        trust.record(conn, "a", "MJ", "maybe", "")
    assert conn.execute("SELECT count(*) FROM reviews").fetchone() == (0,)


def test_a_database_without_reviews_is_judged_on_its_publishers_alone(conn: sqlite3.Connection) -> None:
    add(conn, "a")
    conn.execute("DROP TABLE reviews")
    assert state(conn, "a")[0] == "official"


def test_upgrade_adds_the_reviews_table_to_an_older_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db.connect(path).close()
    old = sqlite3.connect(path)
    old.execute("DROP TABLE reviews")
    old.close()
    db.upgrade(path)
    check = sqlite3.connect(path)
    assert check.execute("SELECT count(*) FROM reviews").fetchone() == (0,)
    check.close()


def statement(conn: sqlite3.Connection, version: str, quote: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO statements (id, chunk_id, verbatim_quote, summary, modality, applies_to, model, "
            "prompt_version, created_at) VALUES (?, ?, ?, 's', 'must', '[]', 'm', 'p', 't')",
            (uuid.uuid4().hex, f"{version}#1", quote),
        )


def test_quality_counts_ingested_verified_and_evidence_statements(conn: sqlite3.Connection) -> None:
    official = add(conn, "act")
    secondary = add(conn, "guide", doc_type="guide")
    add(conn, "empty", checked="2026-03-01T00:00:00Z")
    with conn:
        conn.execute(
            "INSERT INTO documents (id, publisher, title, url, language, doc_type, tags) "
            "VALUES ('bare', 'Parliament', 'T', 'https://example.org/b', 'en', 'act', '[]')"
        )
    statement(conn, official, "Operators must label allergens.")
    statement(conn, official, "Retailers should keep records.")
    statement(conn, official, "Operators must never label allergens.")  # no longer in its section
    statement(conn, secondary, "Operators must label allergens.")

    rows = {r.source_id: r for r in quality.measure(conn, DOMAIN)}
    act = rows["act"]
    assert (act.level, act.sections, act.covered, act.statements, act.verified, act.evidence) == (
        "official", 1, 1, 3, 2, 2,
    )  # fmt: skip
    guide = rows["guide"]
    assert (guide.level, guide.statements, guide.verified, guide.evidence) == ("secondary", 1, 1, 0)
    assert (rows["empty"].statements, rows["empty"].covered, rows["empty"].checked) == (0, 0, "2026-03-01T00:00:00Z")
    assert (rows["bare"].fetched, rows["bare"].checked) == (False, "")

    trust.record(conn, "guide", "MJ", "vetted", "")  # a vetted secondary source is official, but not binding
    assert {r.source_id: r for r in quality.measure(conn, DOMAIN)}["guide"].evidence == 0
    trust.record(conn, "act", "MJ", "disputed", "Looks like a mirror.")
    assert {r.source_id: r for r in quality.measure(conn, DOMAIN)}["act"].evidence == 0


def test_cli_quality_and_review_report_and_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], conn: sqlite3.Connection
) -> None:
    add(conn, "act")
    add(conn, "guide", doc_type="guide")
    statement(conn, "act@2026-01-01T00:00:00Z", "Operators must label allergens.")
    conn.close()
    path = tmp_path / "domain.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Test",
                "instructions": "Test domain.",
                "doc_types": ["act", {"id": "guide", "binding": False, "note": "guidance"}],
                "tags": ["food"],
                "modalities": [{"id": "must", "description": "required"}],
                "topics": [{"id": "labelling", "label": "Labelling", "description": "labels"}],
                "publishers": [{"name": "Parliament", "domains": ["example.org"], "official": True}],
            }
        ),
        encoding="utf-8",
    )
    base = ["--db", str(tmp_path / "kb.db"), "--domain", str(path)]

    assert main([*base, "quality"]) == 0
    out = capsys.readouterr().out
    assert "act" in out and "official" in out and "secondary" in out
    assert "2 sources (1 official, 1 secondary, 0 unverified, 0 disputed); 2 sections, 1 with statements" in out
    assert "1 statements, 1 verified, 1 evidence" in out

    assert main([*base, "review"]) == 0
    assert capsys.readouterr().out.splitlines()[1].startswith("guide")
    assert main([*base, "review", "guide", "--dispute"]) == 1  # a dispute needs its reason
    assert "needs a note" in capsys.readouterr().err
    assert main([*base, "review", "guide", "--dispute", "--note", "A mirror."]) == 0
    out = capsys.readouterr().out
    assert "Tester disputed guide" in out  # the saved name is the reviewer
    assert "trust      disputed: disputed by Tester: A mirror." in out
    assert "disputed Tester  A mirror." in out
    assert main([*base, "review", "nope"]) == 1
    assert "unknown source id: nope" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([*base, "review", "act", "--vet", "--dispute"])  # argparse refuses both verdicts
    capsys.readouterr()
    assert main(["--db", str(tmp_path / "missing.db"), "--domain", str(path), "quality"]) == 1


def test_kb_sources_tell_an_agent_how_far_each_source_is_trusted(conn: sqlite3.Connection) -> None:
    add(conn, "act")
    add(conn, "stranger", publisher="Nobody")
    listed = {s["source_id"]: s for s in search.sources(conn, DOMAIN)["sources"]}  # type: ignore[attr-defined]
    assert listed["act"]["trust"] == "official"
    assert listed["stranger"]["trust"] == "unverified"
    assert "not declared" in listed["stranger"]["trust_reason"]


def test_review_overview_lists_the_trust_of_a_knowledge_base_in_a_directory(tmp_path: Path) -> None:
    (tmp_path / "domain.yaml").write_text((EXAMPLES / "domain.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError, match="does not exist"):
        review.overview(tmp_path)
    connection = db.connect(tmp_path / db.DEFAULT_PATH)
    with connection:
        connection.execute(
            "INSERT INTO documents (id, publisher, title, url, language, doc_type, tags) VALUES "
            "('w3c', 'W3C Technical Architecture Group', 'T', 'https://www.w3.org/TR/x/', 'en', 'guidance', '[]')"
        )
    connection.close()
    [(source_id, found)] = review.overview(tmp_path)
    assert (source_id, found.level) == ("w3c", "official")
