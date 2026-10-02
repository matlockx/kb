import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kb import domain, setup


def answers(*replies: str) -> setup.Ask:
    """An ask() that returns the given replies in order and then reports closed input."""
    queue: Iterator[str] = iter(replies)

    def ask(_prompt: str) -> str:
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    return ask


class Recorder:
    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self.failing = failing

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return 1 if argv[2] in self.failing else 0

    def steps(self) -> list[str]:
        return [argv[2] for argv in self.calls]


GOLDEN = "- question: How long is a taper?\n  expect:\n    - {source: guide, section: '4'}\n"


def test_create_writes_a_valid_template_with_the_answer(tmp_path: Path) -> None:
    home = tmp_path / "running"
    run = Recorder()
    assert setup.setup("running", home, answers("the professional creation of running plans", "n"), run) == 0
    assert run.calls == []  # creating builds nothing
    defined = domain.load(home / "domain.yaml")
    assert defined.name == "running knowledge base"
    assert "Holds documents on the professional creation of running plans:" in defined.instructions
    assert "knowledge base on the professional creation of running plans," in (home / "prompts/extract.md").read_text()
    assert (home / ".gitignore").read_text() == "data/\nraw/\ndownloads/\n"
    assert (home / "eval/golden.yaml").exists()
    assert (home / "sources.yaml").exists()
    assert not (home / ".git").exists()


def test_create_keeps_existing_files_and_defaults_on_closed_input(tmp_path: Path) -> None:
    home = tmp_path / "running"
    home.mkdir()
    (home / "sources.yaml").write_text("# mine\n", encoding="utf-8")
    assert setup.setup("running", home, answers(), Recorder()) == 0
    assert (home / "sources.yaml").read_text() == "# mine\n"
    assert domain.load(home / "domain.yaml").name == "running knowledge base"
    assert not (home / ".git").exists()  # closed input declines


def test_bad_name_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert setup.setup("Running Plans", tmp_path / "x", answers(), Recorder()) == 1
    assert "lowercase letters" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def built(tmp_path: Path, golden: str = "") -> Path:
    home = tmp_path / "running"
    setup.setup("running", home, answers("running plans", "n"), Recorder())
    if golden:
        (home / "eval/golden.yaml").write_text(golden, encoding="utf-8")
    return home


def test_build_stops_when_the_check_fails(tmp_path: Path) -> None:
    home, config = built(tmp_path), tmp_path / "mcp.json"
    run = Recorder(failing=("sources",))
    assert setup.setup("running", home, answers("y"), run, config) == 1
    assert run.steps() == ["sources"]
    assert not config.exists()


def test_build_runs_the_pipeline_and_registers_next_to_other_servers(tmp_path: Path) -> None:
    home, config = built(tmp_path, GOLDEN), tmp_path / "mcp.json"
    config.write_text(json.dumps({"$schema": "s", "mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    run = Recorder()
    assert setup.setup("running", home, answers(""), run, config) == 0
    assert run.steps() == ["sources", "fetch", "parse", "extract", "index", "eval"]
    assert all(argv[:2] == ["-C", str(home.resolve())] for argv in run.calls)
    data = json.loads(config.read_text())
    assert data["$schema"] == "s"
    assert data["mcpServers"]["other"] == {"command": "x"}
    entry = data["mcpServers"]["running"]
    assert entry["args"][-3:] == ["-C", str(home.resolve()), "serve"]
    assert entry["timeout"] == setup.MCP_TIMEOUT_MS

    before = config.read_text()
    assert setup.setup("running", home, answers(), Recorder(), config) == 0  # already registered: no question
    assert config.read_text() == before


def test_build_continues_past_failing_steps_and_skips_an_empty_golden_set(tmp_path: Path) -> None:
    home, config = built(tmp_path), tmp_path / "mcp.json"
    run = Recorder(failing=("fetch",))
    assert setup.setup("running", home, answers("y"), run, config) == 1
    assert run.steps() == ["sources", "fetch", "parse", "extract", "index"]
    assert "running" in json.loads(config.read_text())["mcpServers"]


def test_build_does_not_register_when_index_fails_or_the_user_declines(tmp_path: Path) -> None:
    home, config = built(tmp_path), tmp_path / "mcp.json"
    assert setup.setup("running", home, answers("y"), Recorder(failing=("index",)), config) == 1
    assert not config.exists()
    assert setup.setup("running", home, answers("n"), Recorder(), config) == 0
    assert not config.exists()
    assert setup.setup("running", home, answers(), Recorder(), config) == 0  # closed input declines
    assert not config.exists()


def test_replacing_a_different_entry_asks_and_unreadable_configs_are_left_alone(tmp_path: Path) -> None:
    home, config = built(tmp_path), tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"running": {"command": "old"}}}), encoding="utf-8")
    setup.setup("running", home, answers("n"), Recorder(), config)
    assert json.loads(config.read_text())["mcpServers"]["running"] == {"command": "old"}
    setup.setup("running", home, answers("y"), Recorder(), config)
    assert json.loads(config.read_text())["mcpServers"]["running"]["command"] == "uv"

    config.write_text("{not json", encoding="utf-8")
    assert setup.setup("running", home, answers("y"), Recorder(), config) == 0
    assert config.read_text() == "{not json"
