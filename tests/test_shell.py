import io
import json
import os
import pty
import re
import threading
from pathlib import Path

import pytest

from kb import catalog, cli, db, setup, shell


def keys(*pressed: str) -> shell.Keys:
    """A key source returning the given keys in order, then 'back' for ever (closed input)."""
    queue = iter(pressed)
    return lambda: next(queue, "back")


class Recorder:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return self.code


def make_shell(tmp_path: Path, pressed: list[str], replies: tuple[str, ...] = (), **kwargs) -> shell.Shell:
    queue = iter(replies)

    def ask(_prompt: str) -> str:
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    kwargs.setdefault("cli", Recorder())
    return shell.Shell(
        keys=keys(*pressed), ask=ask, out=io.StringIO(), root=tmp_path / "kbs", config=tmp_path / "mcp.json", **kwargs
    )


def knowledge_base(root: Path, name: str, built: bool = False) -> Path:
    home = root / name
    home.mkdir(parents=True)
    (home / "domain.yaml").write_text("name: x\n", encoding="utf-8")
    if built:
        conn = db.connect(home / db.DEFAULT_PATH)
        conn.close()
    return home


@pytest.mark.parametrize(
    ("data", "key"),
    [(b"\x1b[A", "up"), (b"k", "up"), (b"\x1b[B", "down"), (b"\r", "enter"), (b"\x1b", "back"), (b"x", "")],
)
def test_read_key_names_terminal_input_and_restores_the_mode(data: bytes, key: str) -> None:
    master, slave = pty.openpty()
    try:
        lflags = lambda: shell.termios.tcgetattr(slave)[3] & (shell.termios.ICANON | shell.termios.ECHO)  # noqa: E731
        before = lflags()
        typing = threading.Timer(0.2, os.write, (master, data))  # after read_key left canonical mode
        typing.start()
        assert shell.read_key(slave) == key
        typing.join()
        assert lflags() == before  # line editing and echo are back on (the kernel may add PENDIN)
    finally:
        os.close(master)
        os.close(slave)


def test_choose_moves_wrapping_and_erases_its_frame() -> None:
    items = [shell.Item(label, "", lambda: None) for label in ("a", "b", "c")]
    out = io.StringIO()
    assert shell.choose("t", items, keys("up", "", "down", "down", "enter"), out) == 1
    assert out.getvalue().endswith(shell.SHOW_CURSOR)
    assert "\x1b[J" in out.getvalue().rsplit("\n", 1)[-1]  # the last write clears the frame
    assert shell.choose("t", items, keys("down", "enter"), io.StringIO(), start=9) == 0  # start clamps to the end
    assert shell.choose("t", items, keys("down", "back"), io.StringIO()) is None


def test_render_marks_the_cursor_and_fits_the_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell.shutil, "get_terminal_size", lambda: os.terminal_size((30, 10)))
    items = [shell.Item("short", "x" * 100, lambda: None), shell.Item("other", "", lambda: None)]
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in shell.render("t" * 100, items, 1, "quit")]
    assert all(len(line) < 30 for line in lines)  # a wrapped line would break the in-place redraw
    assert lines[4].startswith("  > other")
    assert lines[3].startswith("    short")


def test_discover_lists_the_root_then_other_registered_knowledge_bases(tmp_path: Path) -> None:
    root = tmp_path / "kbs"
    running = knowledge_base(root, "running")
    (root / "notes").mkdir()  # neither domain.yaml nor a database
    elsewhere = knowledge_base(tmp_path, "elsewhere")
    registered = {
        "running-alias": setup.mcp_entry(running),  # same directory, found under root already
        "web": setup.mcp_entry(elsewhere),
        "other": {"command": "npx", "args": ["-y", "server"]},
        "broken": "not a mapping",
    }
    assert shell.discover(root, registered) == [("running", running.resolve()), ("web", elsewhere.resolve())]
    assert shell.discover(tmp_path / "missing", {}) == []


def test_describe_reports_build_and_registration(tmp_path: Path) -> None:
    home = knowledge_base(tmp_path, "running")
    assert shell.describe("running", home, {}) == "not built · not registered"
    conn = db.connect(home / db.DEFAULT_PATH)
    conn.close()
    assert shell.describe("running", home, {"running": setup.mcp_entry(home)}) == (
        "sources: 0 · statements: 0 · registered"
    )
    assert shell.describe("running", home, {"running": {"command": "x"}}).endswith("registered with another command")
    (home / db.DEFAULT_PATH).write_bytes(b"not a database")
    assert shell.describe("running", home, {}).startswith("unreadable database")


def test_servers_tolerates_missing_and_malformed_configs(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    assert shell.servers(config) == {}
    config.write_text("{not json", encoding="utf-8")
    assert shell.servers(config) == {}
    config.write_text('{"mcpServers": []}', encoding="utf-8")
    assert shell.servers(config) == {}
    config.write_text('{"mcpServers": {"a": {}}}', encoding="utf-8")
    assert shell.servers(config) == {"a": {}}


def test_a_step_runs_the_kb_command_for_that_directory_and_keeps_the_cursor(tmp_path: Path) -> None:
    home = knowledge_base(tmp_path / "kbs", "running")
    recorder = Recorder()
    # open running, move to fetch, run it, run it again (cursor stays on fetch), back, quit
    s = make_shell(tmp_path, ["enter", "down", "down", "enter", "enter", "back", "back"], cli=recorder)
    assert s.run() == 0
    assert recorder.calls == [["-C", str(home.resolve()), "fetch"]] * 2
    assert s.out.getvalue().count("✓ running > fetch") == 2


def test_build_runs_setup_and_register_writes_the_omp_config(tmp_path: Path) -> None:
    home = knowledge_base(tmp_path / "kbs", "running")
    recorder = Recorder()
    s = make_shell(tmp_path, ["enter", "down", "enter", "up", "up", "enter"], replies=("y",), cli=recorder)
    s.run()
    assert recorder.calls[0] == ["-C", str(home.resolve()), "sources", "--check"]  # setup.build's first step
    assert json.loads(s.config.read_text())["mcpServers"]["running"] == setup.mcp_entry(home.resolve())


def test_new_creates_from_the_template_and_an_empty_name_cancels(tmp_path: Path) -> None:
    s = make_shell(tmp_path, ["enter", "down", "enter"], replies=("running", "running plans", "n", ""))
    s.run()
    assert (tmp_path / "kbs" / "running" / "domain.yaml").exists()
    assert sorted(p.name for p in (tmp_path / "kbs").iterdir()) == ["running"]


def test_failures_are_reported_and_return_to_the_menu(capsys: pytest.CaptureFixture[str]) -> None:
    out = io.StringIO()
    shell.perform("x", lambda: 1, out)
    shell.perform("y", lambda: (_ for _ in ()).throw(SystemExit(2)), out)
    shell.perform("z", lambda: (_ for _ in ()).throw(KeyboardInterrupt()), out)
    shell.perform("w", lambda: (_ for _ in ()).throw(ValueError("boom")), out)
    shell.perform("v", lambda: None, out)
    text = out.getvalue()
    assert "✗ x failed (1)" in text
    assert "✗ y failed (2)" in text
    assert "✗ z failed (interrupted)" in text
    assert "✗ w failed (error)" in text
    assert "✓ v" in text
    assert "ValueError: boom" in capsys.readouterr().err


def test_ctrl_c_in_a_menu_quits(tmp_path: Path) -> None:
    def interrupt() -> str:
        raise KeyboardInterrupt

    s = make_shell(tmp_path, [])
    s.keys = interrupt
    assert s.run() == 0


def test_catalog_items_pull_and_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shelf = tmp_path / "shelf"
    for name in ("fresh", "running"):
        (shelf / name).mkdir(parents=True)
        (shelf / name / catalog.MANIFEST).write_text("{}", encoding="utf-8")
    home = knowledge_base(tmp_path / "kbs", "running")
    pulled, published = [], []
    monkeypatch.setattr(
        catalog, "pull", lambda name, _c, home, force, _a, index, _o: pulled.append((name, home, force, index(home)))
    )
    monkeypatch.setattr(catalog, "publish", lambda home, c, name, *_: published.append((home, c, name)))
    recorder = Recorder()
    # home menu: running, + new, ↓ pull, age key. Pull fresh, then running (an update), then publish running.
    pressed = ["down", "down", "enter", "enter", "enter", "down", "enter", "up", "up", "enter", "up", "enter"]
    s = make_shell(tmp_path, pressed, cli=recorder, shelf=shelf)
    s.run()
    assert pulled == [
        ("fresh", tmp_path / "kbs" / "fresh", False, 0),
        ("running", tmp_path / "kbs" / "running", True, 0),
    ]
    assert recorder.calls[0] == ["--db", str(tmp_path / "kbs" / "fresh"), "index"]
    assert published == [(home.resolve(), shelf, "running")]


def test_an_empty_catalog_says_so(tmp_path: Path) -> None:
    (tmp_path / "shelf").mkdir()
    s = make_shell(tmp_path, ["down", "enter"], shelf=tmp_path / "shelf")
    s.run()
    assert "no knowledge bases in" in s.out.getvalue()


def test_bare_kb_needs_a_terminal(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "a command is required without a terminal" in capsys.readouterr().err


def test_a_directory_opens_its_own_menu_under_its_registered_name(tmp_path: Path) -> None:
    elsewhere = knowledge_base(tmp_path, "elsewhere")
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"web": setup.mcp_entry(elsewhere)}}))
    recorder = Recorder()
    s = make_shell(tmp_path, ["enter"], cli=recorder)  # check, then back leaves the shell
    assert s.run(elsewhere) == 0
    assert recorder.calls == [["-C", str(elsewhere.resolve()), "sources", "--check"]]
    assert "web · not built · registered" in s.out.getvalue()
    unregistered = knowledge_base(tmp_path, "notes")
    s = make_shell(tmp_path, [])
    s.run(unregistered)
    assert "notes · not built · not registered" in s.out.getvalue()  # named after the directory


def test_kb_with_only_a_directory_opens_that_menu_on_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = []
    monkeypatch.setattr(shell, "run", lambda _cli, home=None: opened.append(home) or 0)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    assert cli.main(["-C", str(tmp_path)]) == 0
    assert cli.main([]) == 0
    assert opened == [tmp_path, None]
