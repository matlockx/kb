import json
from pathlib import Path
from typing import LiteralString

import pytest
import yaml

from kb import db, sources
from kb.cli import main
from kb.domain import ConfigError, Domain, Modality, Topic

DOMAIN = Domain(
    name="Test",
    instructions="Test domain.",
    doc_types=("act", "guidance"),
    tags=("lottery", "casino"),
    modalities=(Modality("must", "Required."),),
    topics=(Topic("licensing", "Licensing", "Licences."),),
)
ORIGINAL = {
    "id": "se-act",
    "publisher": "Riksdag",
    "title": "Spellag",
    "url": "https://example.org/sv",
    "language": "sv",
    "doc_type": "act",
    "tags": ["lottery", "casino"],
}


def write(tmp_path: Path, entries: object) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return path


def errors_for(tmp_path: Path, entries: object) -> str:
    with pytest.raises(ConfigError) as exc:
        sources.load(write(tmp_path, entries), DOMAIN)
    return str(exc.value)


def test_parse_fields_load(tmp_path: Path) -> None:
    entry = {
        **ORIGINAL,
        "section_pattern": r"^(?P<ref>\d+ §)",
        "chapter_pattern": r"^(?P<ref>\d+ kap\.)",
        "body_start": "^1 kap",
        "chapter_label": "k",
        "first_chapter": "LC",
    }
    [source] = sources.load(write(tmp_path, [entry]), DOMAIN)
    assert (source.section_pattern, source.body_start, source.first_chapter) == (r"^(?P<ref>\d+ §)", "^1 kap", "LC")


def test_valid_entries_load(tmp_path: Path) -> None:
    second = {**ORIGINAL, "id": "se-guide", "doc_type": "guidance", "tags": ["casino"]}
    found = sources.load(write(tmp_path, [ORIGINAL, second]), DOMAIN)
    assert [s.id for s in found] == ["se-act", "se-guide"]
    assert found[0].tags == ("lottery", "casino")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": "SE Act"}, "id must be"),
        ({"language": "swe"}, "language must be"),
        ({"doc_type": "blog"}, "doc_type must be"),
        ({"skip_sections": "("}, "skip_sections"),
        ({"url": "http://example.org"}, "https URL"),
        ({"url": "https:///no-host"}, "https URL"),
        ({"tags": []}, "tags must be"),
        ({"tags": ["slots"]}, "unknown tags"),
        ({"tags": ["casino", "casino"]}, "duplicates"),
        ({"title": "  "}, "title must be a non-empty string"),
        ({"extra": 1}, "unknown field 'extra'"),
        ({"section_pattern": "(unclosed"}, "section_pattern is not a valid regex"),
        ({"section_pattern": r"^\d+"}, "needs a named group (?P<ref>...)"),
        ({"chapter_pattern": ""}, "chapter_pattern must be a non-empty regex string"),
        ({"chapter_label": "code"}, "need a chapter_pattern"),
        ({"section_label": "x"}, "section_label needs a section_pattern"),
        (
            {"section_pattern": r"^(?P<ref>(?P<num>\d+) ?§)", "section_label": r"\g<nope> §"},
            "section_label does not fit section_pattern",
        ),
        ({"section_pattern": r"^(?P<ref>\d+)", "section_label": r"\q"}, "section_label does not fit section_pattern"),
        ({"chapter_pattern": r"^(?P<ref>\d+)", "chapter_label": r"\3"}, "chapter_label does not fit chapter_pattern"),
        ({"chapter_pattern": "^(?P<ref>X)$", "first_chapter": " "}, "first_chapter must be a non-empty string"),
    ],
)
def test_entry_rejected(tmp_path: Path, change: dict[str, object], message: str) -> None:
    assert message in errors_for(tmp_path, [{**ORIGINAL, **change}])


def test_duplicate_id_and_all_errors_reported(tmp_path: Path) -> None:
    message = errors_for(tmp_path, [ORIGINAL, ORIGINAL, {**ORIGINAL, "id": "x", "language": "xyz"}])
    assert "duplicate id 'se-act'" in message
    assert "entry 3 (x): language must be" in message


@pytest.mark.parametrize("content", ["", "{}", "- just a string", "[unclosed"])
def test_malformed_file_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        sources.load(path, DOMAIN)


def test_sync_upserts_and_reports_stale(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data" / "kb.db")
    second = {**ORIGINAL, "id": "se-guide"}
    assert sources.sync(conn, sources.load(write(tmp_path, [ORIGINAL, second]), DOMAIN)) == []

    renamed = {**ORIGINAL, "title": "Spellag (2018:1138)"}
    assert sources.sync(conn, sources.load(write(tmp_path, [renamed]), DOMAIN)) == ["se-guide"]
    title, tags = conn.execute("SELECT title, tags FROM documents WHERE id = 'se-act'").fetchone()
    assert (title, json.loads(tags)) == ("Spellag (2018:1138)", ["lottery", "casino"])


def write_domain(tmp_path: Path) -> Path:
    domain_file = tmp_path / "domain.yaml"
    domain_file.write_text(
        yaml.safe_dump(
            {
                "name": DOMAIN.name,
                "instructions": DOMAIN.instructions,
                "doc_types": list(DOMAIN.doc_types),
                "tags": list(DOMAIN.tags),
                "modalities": [{"id": m.id, "description": m.description} for m in DOMAIN.modalities],
                "topics": [{"id": t.id, "label": t.label, "description": t.description} for t in DOMAIN.topics],
            }
        ),
        encoding="utf-8",
    )
    return domain_file


def test_cli_check_and_sync(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, [ORIGINAL])
    domain_file = write_domain(tmp_path)
    database = tmp_path / "kb.db"
    args = ["--db", str(database), "--domain", str(domain_file), "sources", "--file", str(path)]
    assert main([*args, "--check"]) == 0
    assert not database.exists()
    assert main(args) == 0
    assert "1 sources and 1 topics synced" in capsys.readouterr().out
    assert conn_rows(database, "SELECT id FROM documents") == [("se-act",)]

    bad = write(tmp_path, [{**ORIGINAL, "language": "xyz"}])
    assert main(["--db", str(database), "--domain", str(domain_file), "sources", "--file", str(bad)]) == 1
    assert "language must be" in capsys.readouterr().err


def conn_rows(database: Path, sql: LiteralString) -> list[tuple[object, ...]]:
    conn = db.connect(database)
    try:
        return sorted(conn.execute(sql).fetchall())
    finally:
        conn.close()


def test_cli_resolves_default_paths_in_the_c_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "running"
    home.mkdir()
    write(home, [ORIGINAL])
    write_domain(home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # restores the working directory main() changes
    assert main(["-C", str(home), "sources"]) == 0
    assert conn_rows(home / "data" / "kb.db", "SELECT id FROM documents") == [("se-act",)]
    assert not (elsewhere / "data").exists()
    assert main(["-C", str(tmp_path / "missing"), "sources", "--check"]) == 1
    assert "-C " in capsys.readouterr().err
