import dataclasses
import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from kb import db, parse, sources, statements
from kb.chunk import Section
from kb.domain import ConfigError, Domain, Modality, Topic
from kb.sources import Source
from kb.statements import ExtractionError

DOMAIN = Domain(
    name="Test",
    instructions="Test domain.",
    doc_types=("act", "regulation"),
    tags=("food", "toys", "cosmetics"),
    modalities=(Modality("must", "required"), Modality("should", "advised"), Modality("may", "allowed")),
    topics=(Topic("labelling", "Labelling", "d"), Topic("allergens", "Allergens", "d")),
)
TOPIC_IDS = frozenset({"labelling", "allergens"})
MODALITIES = frozenset({"must", "should", "may"})
SOURCE = Source(
    id="ie-si",
    publisher="FSAI",
    title="S.I. 315",
    url="https://example.org/si",
    language="en",
    doc_type="regulation",
    tags=("food", "toys"),
)
TEXT = (
    "3. A producer labels the product where—\n(a) the product contains an allergen listed in a schedule, and\n"
    "(b) more, within 2-3 days."
)
RECORD = {
    "verbatim_quote": "the product contains an allergen listed in a schedule,",
    "summary": "One trigger is a listed allergen.",
    "modality": "may",
    "topics": ["labelling", "allergens"],
    "applies_to": ["food"],
}


def domain_yaml(domain: Domain) -> str:
    return yaml.safe_dump(
        {
            "name": domain.name,
            "instructions": domain.instructions,
            "doc_types": list(domain.doc_types),
            "tags": list(domain.tags),
            "modalities": [dataclasses.asdict(m) for m in domain.modalities],
            "topics": [dataclasses.asdict(t) for t in domain.topics],
        }
    )


@pytest.fixture
def system(tmp_path: Path) -> str:
    prompt = tmp_path / "extract.md"
    prompt.write_text("Extract statements.\n", encoding="utf-8")
    return statements.system_prompt(prompt, DOMAIN)


def test_system_prompt_appends_modalities_then_topics(tmp_path: Path, system: str) -> None:
    assert system == (
        "Extract statements.\n\nModalities:\n- must: required\n- should: advised\n- may: allowed\n"
        "\nTopics:\n- labelling: d\n- allergens: d\n"
    )
    changed = dataclasses.replace(DOMAIN, topics=(Topic("labelling", "Labelling", "other"), DOMAIN.topics[1]))
    assert statements.prompt_version(statements.system_prompt(tmp_path / "extract.md", changed)) != (
        statements.prompt_version(system)
    )
    with pytest.raises(ConfigError, match=r"missing\.md"):
        statements.system_prompt(tmp_path / "missing.md", DOMAIN)


@pytest.mark.parametrize(
    "quote",
    [
        "Sikkerhedsstyrelsen skal",
        "Sikkerheds-styrelsen skal",
        "Sikkerheds- styrelsen skal",
        "Sikkerhedsstyrel sen skal",
    ],
)
def test_validate_accepts_quote_across_hyphenated_line_break(quote: str) -> None:
    text = "Virksomheden og Sikkerheds-\nstyrelsen skal aftale."
    record = {**RECORD, "verbatim_quote": quote, "applies_to": ["food"]}
    assert statements.validate(record, text, SOURCE, TOPIC_IDS, MODALITIES).verbatim_quote == quote


def test_validate_accepts_hyphen_after_space_at_line_break() -> None:
    text = "under garantipe -\nrioden gäller"
    record = {**RECORD, "verbatim_quote": "under garantiperioden gäller"}
    assert statements.validate(record, text, SOURCE, TOPIC_IDS, MODALITIES)


def test_validate_accepts_quote_across_line_breaks() -> None:
    record = {**RECORD, "verbatim_quote": "product where— (a) the product"}
    kept = statements.validate(record, TEXT, SOURCE, TOPIC_IDS, MODALITIES)
    assert (kept.verbatim_quote, kept.topics, kept.applies_to) == (
        "product where— (a) the product",
        ("labelling", "allergens"),
        ("food",),
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"verbatim_quote": "the product contains an allergen in a schedule"}, "not found in the section"),
        ({"verbatim_quote": "within 23 days"}, "not found in the section"),
        ({"verbatim_quote": "x" * 1001}, "longer than 1000"),
        ({"verbatim_quote": " "}, "verbatim_quote missing"),
        ({"summary": ""}, "summary missing"),
        ({"modality": "shall"}, "modality 'shall'"),
        ({"topics": ["labelling", "payments"]}, "known topic ids"),
        ({"topics": []}, "known topic ids"),
        ({"applies_to": ["cosmetics"]}, "subset of"),
    ],
)
def test_validate_rejects(change: dict[str, object], message: str) -> None:
    with pytest.raises(ExtractionError, match=message):
        statements.validate({**RECORD, **change}, TEXT, SOURCE, TOPIC_IDS, MODALITIES)


@pytest.mark.parametrize(
    ("raw", "count"),
    [('{"statements": []}', 0), ('```json\n{"statements": [{}]}\n```', 1)],
)
def test_parse_output(raw: str, count: int) -> None:
    assert len(statements.parse_output(raw)) == count


@pytest.mark.parametrize("raw", ["Here you go: {}", '{"items": []}', "[]"])
def test_parse_output_rejects(raw: str) -> None:
    with pytest.raises(ExtractionError):
        statements.parse_output(raw)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(tmp_path / "kb.db")
    sources.sync(conn, [SOURCE])
    statements.sync_topics(conn, DOMAIN.topics)
    with conn:
        conn.execute(
            "INSERT INTO document_versions (id, document_id, sha256, raw_path, fetched_at, last_checked_at) "
            "VALUES ('ie-si@v1', 'ie-si', 'x', 'x', 't', 't')"
        )
    parse.store(conn, "ie-si@v1", [Section("(preamble)", (), "front matter"), Section("3", ("S.I.",), TEXT)])
    yield conn
    conn.close()


class FakeModel:
    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.messages: list[str] = []

    def __call__(self, system: str, text: str, model: str) -> str:
        assert "Topics:\n- labelling: d\n- allergens: d\n" in system
        assert model == "m"
        self.messages.append(text)
        return self.outputs.pop(0)


def run(conn: sqlite3.Connection, system: str, model: FakeModel) -> statements.Report:
    [report] = statements.extract_all(
        conn, [SOURCE], DOMAIN, system, "m", 2, model, clock=lambda: datetime(2026, 9, 24, tzinfo=UTC)
    )
    return report


def test_extract_stores_valid_records_and_caches(conn: sqlite3.Connection, system: str) -> None:
    bad = {**RECORD, "verbatim_quote": "invented text"}
    model = FakeModel(json.dumps({"statements": [RECORD, bad]}))
    report = run(conn, system, model)
    assert (report.sections, report.called, report.cached, report.statements) == (1, 1, 0, 1)
    assert report.rejected == ["3: verbatim_quote not found in the section: 'invented text'"]
    [message] = model.messages
    assert "Section: 3\nHeadings: S.I.\n\n<section>\n3. A producer" in message
    assert "front matter" not in message

    rows = conn.execute("SELECT id, modality, applies_to, model, created_at FROM statements").fetchall()
    assert rows == [("ie-si@v1#0001/1", "may", '["food"]', "m", "2026-09-24T00:00:00Z")]
    topics = conn.execute("SELECT topic_id FROM statement_topics ORDER BY topic_id").fetchall()
    assert topics == [("allergens",), ("labelling",)]

    again = run(conn, system, FakeModel())  # no outputs left: a model call would fail
    assert (again.cached, again.called, again.statements) == (1, 0, 1)
    assert conn.execute("SELECT count(*) FROM statements").fetchone() == (1,)


def test_extract_matching_skips_other_sections(conn: sqlite3.Connection, system: str) -> None:
    # no outputs: any model call would fail
    [report] = statements.extract_all(conn, [SOURCE], DOMAIN, system, "m", 1, FakeModel(), matching=r"(?i)duty")
    assert (report.sections, report.called, report.failed) == (0, 0, [])
    model = FakeModel(json.dumps({"statements": [RECORD]}))
    [report] = statements.extract_all(conn, [SOURCE], DOMAIN, system, "m", 1, model, matching=r"A producer")
    assert (report.sections, report.called, report.statements) == (1, 1, 1)


def test_extract_skip_sections_drops_earlier_statements(conn: sqlite3.Connection, system: str) -> None:
    run(conn, system, FakeModel(json.dumps({"statements": [RECORD]})))
    assert conn.execute("SELECT count(*) FROM statements").fetchone() == (1,)
    skipping = dataclasses.replace(SOURCE, skip_sections=r"3\b")
    [report] = statements.extract_all(conn, [skipping], DOMAIN, system, "m", 1, FakeModel())  # no outputs: no calls
    assert (report.sections, report.skipped, report.called) == (0, 1, 0)
    assert conn.execute("SELECT count(*) FROM statements").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM statement_topics").fetchone() == (0,)


def test_extract_retries_once_then_reports_failure(
    conn: sqlite3.Connection, system: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(statements, "RETRY_DELAY_S", 0)
    report = run(conn, system, FakeModel("not json", json.dumps({"statements": [RECORD]})))
    assert (report.called, report.statements, report.failed) == (1, 1, [])

    conn.execute("DELETE FROM extraction_cache")
    failed = run(conn, system, FakeModel("nope", "still nope"))
    assert failed.failed == ["3: output is not JSON: 'still nope'"]
    assert conn.execute("SELECT count(*) FROM statements").fetchone() == (1,)  # earlier result untouched
    assert conn.execute("SELECT count(*) FROM extraction_cache").fetchone() == (0,)


def test_cli_extract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from kb.cli import main

    entry = {k: v for k, v in SOURCE.__dict__.items() if v not in (None, "", ())} | {"tags": ["food", "toys"]}
    (tmp_path / "sources.yaml").write_text(yaml.safe_dump([entry]), encoding="utf-8")
    (tmp_path / "domain.yaml").write_text(domain_yaml(DOMAIN), encoding="utf-8")
    (tmp_path / "extract.md").write_text("Extract statements.\n", encoding="utf-8")
    database = tmp_path / "kb.db"
    conn = db.connect(database)
    sources.sync(conn, [SOURCE])
    with conn:
        conn.execute(
            "INSERT INTO document_versions (id, document_id, sha256, raw_path, fetched_at, last_checked_at) "
            "VALUES ('ie-si@v1', 'ie-si', 'x', 'x', 't', 't')"
        )
    parse.store(conn, "ie-si@v1", [Section("3", (), TEXT)])
    conn.close()
    args = ["--db", str(database), f"--domain={tmp_path / 'domain.yaml'}", "extract", "--model", "m"]
    args += ["--workers", "1", f"--file={tmp_path / 'sources.yaml'}", f"--prompt={tmp_path / 'extract.md'}"]

    def missing_pi(*_: str) -> str:
        raise FileNotFoundError("pi")

    monkeypatch.setattr(statements, "RETRY_DELAY_S", 0)
    monkeypatch.setattr(statements, "call_pi", missing_pi)
    assert main(args) == 1
    assert "  3: pi" in capsys.readouterr().out

    monkeypatch.setattr(statements, "call_pi", FakeModel(json.dumps({"statements": [RECORD]})))
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "ie-si                                     1      0      1     1        0      0" in out
    assert out.rstrip().endswith("1 statements, 0 rejected, 0 sections failed")

    monkeypatch.setattr(statements, "call_pi", FakeModel())  # no outputs: any model call would fail
    assert main([*args, "--matching", "(?i)excise duty"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("0 statements, 0 rejected, 0 sections failed")
    conn = db.connect(database)
    assert conn.execute("SELECT count(*) FROM statements").fetchone() == (1,)  # unmatched section kept
    conn.close()
    with pytest.raises(SystemExit):
        main([*args, "--matching", "("])
    assert "invalid regex" in capsys.readouterr().err


def test_pi_extensions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_PI_EXTENSIONS", raising=False)
    monkeypatch.setattr(statements, "ANTHROPIC_AUTH", tmp_path / "missing")
    assert statements.pi_extensions() == []
    monkeypatch.setattr(statements, "ANTHROPIC_AUTH", tmp_path)
    assert statements.pi_extensions() == [str(tmp_path)]
    monkeypatch.setenv("KB_PI_EXTENSIONS", "")
    assert statements.pi_extensions() == []
    monkeypatch.setenv("KB_PI_EXTENSIONS", f"/a{os.pathsep}/b")
    assert statements.pi_extensions() == ["/a", "/b"]


def test_call_pi_passes_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"statements": []}', "")

    monkeypatch.delenv("KB_PI_PREFIX", raising=False)
    monkeypatch.setenv("KB_PI_EXTENSIONS", "/ext/auth")
    monkeypatch.setattr(statements.subprocess, "run", fake_run)
    assert statements.call_pi("sys", "msg", "m") == '{"statements": []}'
    [(command, kwargs)] = seen
    assert kwargs["stdin"] is subprocess.DEVNULL  # pi -p blocks reading an inherited non-TTY stdin
    assert "--no-extensions" in command
    assert command[command.index("-e") + 1] == "/ext/auth"
    assert command[-3:] == ["--system-prompt", "sys", "msg"]


def test_message() -> None:
    assert statements.message(SOURCE, "3", [], "Advertisers should comply.") == (
        "Document: S.I. 315 (FSAI)\nType: regulation\nLanguage: en\nTags: food, toys\nSection: 3\nHeadings: -\n\n"
        "<section>\nAdvertisers should comply.\n</section>"
    )
