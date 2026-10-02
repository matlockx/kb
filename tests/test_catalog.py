import sqlite3
import subprocess
from pathlib import Path

import pytest

from kb import catalog, db, setup


def closed(_prompt: str) -> str:
    raise EOFError


def built(home: Path, marker: str = "v1") -> Path:
    """A knowledge base directory with its configuration, a database holding marker, and a raw download."""
    for rel in ("domain.yaml", "sources.yaml", "prompts/extract.md", "eval/golden.yaml"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(f"name: {home.name} knowledge base\n" if rel == "domain.yaml" else "x\n")
    (home / "raw").mkdir()
    (home / "raw" / "big.pdf").write_bytes(b"pdf")
    conn = db.connect(home / db.DEFAULT_PATH)
    conn.execute("CREATE TABLE marker (value TEXT)")
    conn.execute("INSERT INTO marker VALUES (?)", (marker,))
    conn.commit()
    conn.close()
    return home


def marker(home: Path) -> str:
    conn = sqlite3.connect(home / db.DEFAULT_PATH)
    try:
        return conn.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        conn.close()


def pull(name: str, shelf: Path, home: Path, force: bool = False, warm=lambda: None) -> int:
    return catalog.pull(name, shelf, home, force, closed, home.parent / "mcp.json", warm)


def test_publish_and_pull_copy_config_and_database_but_not_raw(tmp_path: Path, capsys) -> None:
    shelf = tmp_path / "catalog"
    shelf.mkdir()
    assert catalog.publish(built(tmp_path / "running"), shelf, "running") == 0
    assert not (shelf / "running" / "raw").exists()
    assert [p.name for p in shelf.iterdir()] == ["running"]  # no staging leftovers

    target = tmp_path / "device" / "running"
    assert pull("running", shelf, target) == 0
    assert marker(target) == "v1"
    assert (target / "prompts" / "extract.md").read_text() == "x\n"
    assert not (target / "raw").exists()

    assert catalog.show(shelf, tmp_path / "device") == 0
    line = capsys.readouterr().out.splitlines()[-1]
    assert line.startswith("running ")
    assert "installed" in line
    assert line.endswith("running knowledge base")


def test_pull_replaces_an_existing_knowledge_base_only_with_force(tmp_path: Path) -> None:
    shelf = tmp_path / "catalog"
    shelf.mkdir()
    source = built(tmp_path / "running", "v2")
    assert catalog.publish(source, shelf, "running") == 0
    local = built(tmp_path / "device" / "running", "v1")

    assert pull("running", shelf, local) == 1
    assert marker(local) == "v1"
    assert pull("running", shelf, local, force=True) == 0
    assert marker(local) == "v2"
    assert (local / "raw" / "big.pdf").exists()  # local downloads survive
    assert not [p for p in local.iterdir() if p.name.startswith(".")]


def test_publish_refuses_an_unbuilt_knowledge_base_and_pull_an_unknown_name(tmp_path: Path) -> None:
    shelf = tmp_path / "catalog"
    shelf.mkdir()
    home = built(tmp_path / "running")
    (home / db.DEFAULT_PATH).unlink()
    assert catalog.publish(home, shelf, "running") == 1
    assert catalog.publish(built(tmp_path / "other"), shelf, "Bad Name") == 1
    assert list(shelf.iterdir()) == []
    assert pull("running", shelf, tmp_path / "device" / "running") == 1


def test_pull_keeps_the_copy_when_the_model_download_fails(tmp_path: Path, capsys) -> None:
    shelf = tmp_path / "catalog"
    shelf.mkdir()
    catalog.publish(built(tmp_path / "running"), shelf, "running")

    def offline() -> None:
        raise OSError("no network")

    target = tmp_path / "device" / "running"
    assert pull("running", shelf, target, warm=offline) == 0
    assert marker(target) == "v1"
    assert "embedding model is not cached (no network)" in capsys.readouterr().err


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = t\n\temail = t@example.org\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607


@pytest.mark.usefixtures("git_env")
def test_git_catalog_pushes_on_publish_and_pulls_before_reading(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote))
    laptop, phone = tmp_path / "laptop", tmp_path / "desk"
    git("clone", "-q", str(remote), str(laptop))
    (laptop / "README").write_text("catalog\n")
    git("-C", str(laptop), "add", "README")
    git("-C", str(laptop), "commit", "-qm", "init")
    git("-C", str(laptop), "push", "-q", "origin", "main")
    git("clone", "-q", str(remote), str(phone))

    source = built(tmp_path / "running")
    assert catalog.publish(source, laptop, "running") == 0
    assert git("-C", str(laptop), "status", "--porcelain") == ""
    assert catalog.publish(source, laptop, "running") == 0  # unchanged: no empty commit
    assert git("-C", str(remote), "log", "--format=%s") == "Publish running\ninit\n"

    target = tmp_path / "device" / "running"
    assert pull("running", phone, target) == 0  # the second clone pulls the new copy first
    assert marker(target) == "v1"


def test_cli_reads_the_catalog_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from kb import cli

    shelf = tmp_path / "catalog"
    shelf.mkdir()
    home = built(tmp_path / "running")
    monkeypatch.setenv("KB_CATALOG", str(shelf))
    monkeypatch.chdir(tmp_path)  # -C changes the working directory
    monkeypatch.setattr(setup, "KB_ROOT", tmp_path / "none")
    assert cli.main(["-C", str(home), "publish"]) == 0
    assert (shelf / "running" / db.DEFAULT_PATH).exists()
    capsys.readouterr()
    assert cli.main(["catalog"]) == 0
    assert capsys.readouterr().out.startswith("running ")
