import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from kb import bundle, catalog, db, keys, setup


def closed(_prompt: str) -> str:
    raise EOFError


@pytest.fixture(autouse=True)
def identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """This device's age identity, outside the real home; returns its public key."""
    path = tmp_path / "keys" / "me.txt"
    monkeypatch.setenv(keys.IDENTITY_ENV, str(path))
    return keys.generate(path)


def use_identity(monkeypatch: pytest.MonkeyPatch, path: Path) -> str:
    monkeypatch.setenv(keys.IDENTITY_ENV, str(path))
    return keys.generate(path) if not path.exists() else keys.public_keys(keys.load(path))[0]


def built(home: Path, marker: str = "v1") -> Path:
    """A built knowledge base: configuration, a database holding marker and one vector, and a raw download."""
    for rel in ("domain.yaml", "sources.yaml", "prompts/extract.md", "eval/golden.yaml"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(f"name: {home.name} knowledge base\n" if rel == "domain.yaml" else f"{marker}\n")
    (home / "raw").mkdir(exist_ok=True)
    (home / "raw" / "big.pdf").write_bytes(b"pdf")
    conn = db.connect(home / db.DEFAULT_PATH)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS marker (value TEXT)")
        conn.execute("DELETE FROM marker")
        conn.execute("INSERT INTO marker VALUES (?)", (marker,))
        conn.execute("INSERT OR REPLACE INTO vectors VALUES ('chunk', 'c1', 'm', 'h', x'00000000')")
    conn.close()
    return home


def marker(home: Path) -> str:
    conn = sqlite3.connect(home / db.DEFAULT_PATH)
    try:
        return conn.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        conn.close()


def vectors(home: Path) -> int:
    conn = sqlite3.connect(home / db.DEFAULT_PATH)
    try:
        return conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
    finally:
        conn.close()


def publish(home: Path, shelf: Path, add: list[str] | None = None, private: bool | None = None) -> int:
    return catalog.publish(home, shelf, "running", add or [], private)


def pull(shelf: Path, home: Path, force: bool = False, indexed: list[Path] | None = None) -> int:
    def index(path: Path) -> int:
        (indexed if indexed is not None else []).append(path)
        return 0

    return catalog.pull("running", shelf, home, force, closed, index, home.parent / "mcp.json")


def manifest(shelf: Path) -> dict:
    return json.loads((shelf / "running" / catalog.MANIFEST).read_text())


@pytest.fixture
def shelf(tmp_path: Path) -> Path:
    path = tmp_path / "catalog"
    path.mkdir()
    return path


def test_publish_encrypts_a_slim_snapshot_and_pull_restores_it(
    tmp_path: Path, shelf: Path, identity: str, capsys
) -> None:
    assert publish(built(tmp_path / "running"), shelf) == 0
    entry = shelf / "running"
    found = manifest(shelf)
    assert (found["version"], found["recipients"], found["title"]) == (1, [identity], "running knowledge base")
    packed = (entry / found["bundle"]["file"]).read_bytes()
    assert packed.startswith(b"age-encryption.org/v1") and b"SQLite format" not in packed
    assert (entry / "sources.yaml").read_text() == "v1\n"  # the readable recipe of a public knowledge base
    assert not (entry / "raw").exists()
    assert sorted(p.name for p in shelf.iterdir()) == ["running"]  # no staging leftovers

    target, indexed = tmp_path / "device" / "running", []
    assert pull(shelf, target, indexed=indexed) == 0
    assert marker(target) == "v1"
    assert vectors(target) == 0  # derived: rebuilt by the index step
    assert indexed == [target / db.DEFAULT_PATH]
    assert (target / "prompts" / "extract.md").read_text() == "v1\n"
    assert not (target / "raw").exists()
    assert bundle.meta(target / db.DEFAULT_PATH)["version"] == "1"

    assert catalog.show(shelf, tmp_path / "device") == 0
    line = capsys.readouterr().out.splitlines()[-1]
    assert line.split()[:2] == ["running", "v1"]
    assert line.endswith("v1             running knowledge base")


def test_unchanged_content_is_not_republished_and_force_pull_updates_reusing_vectors(
    tmp_path: Path, shelf: Path, capsys
) -> None:
    source = built(tmp_path / "running")
    assert publish(source, shelf) == 0
    assert publish(source, shelf) == 0
    assert "v1 is unchanged" in capsys.readouterr().out
    local = tmp_path / "device" / "running"
    assert pull(shelf, local) == 0
    built(local, "local")  # stands in for the vectors and raw files `kb index` and `kb fetch` leave behind

    built(source, "v2")
    assert publish(source, shelf) == 0
    assert manifest(shelf)["version"] == 2
    assert pull(shelf, local) == 1  # an installed knowledge base is replaced only with force
    assert marker(local) == "local"
    assert pull(shelf, local, force=True) == 0
    assert (marker(local), vectors(local)) == ("v2", 1)
    assert (local / "sources.yaml").read_text() == "v2\n"
    assert (local / "raw" / "big.pdf").exists()
    assert not [p for p in local.iterdir() if p.name.startswith(".")]


def test_only_recipients_can_pull_and_a_recipient_added_later_can(
    tmp_path: Path, shelf: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source = built(tmp_path / "running")
    assert publish(source, shelf) == 0
    mine = Path(keys.identity_path())
    colleague = use_identity(monkeypatch, tmp_path / "keys" / "colleague.txt")
    target = tmp_path / "colleague" / "running"
    assert pull(shelf, target) == 1
    assert "is your public key in its recipients" in capsys.readouterr().err
    assert not (target / db.DEFAULT_PATH).exists()

    use_identity(monkeypatch, mine)
    assert publish(source, shelf, add=[colleague]) == 0  # same content, new recipient: a new version
    assert manifest(shelf)["version"] == 2
    assert colleague in (shelf / "running" / catalog.RECIPIENTS).read_text()
    use_identity(monkeypatch, tmp_path / "keys" / "colleague.txt")
    assert pull(shelf, target) == 0
    assert marker(target) == "v1"


def test_a_revoked_key_leaves_the_recipients_and_your_own_key_stays(
    tmp_path: Path, shelf: Path, identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kb import cli

    source = built(tmp_path / "running")
    colleague = keys.generate(tmp_path / "keys" / "colleague.txt")
    assert publish(source, shelf, add=[colleague]) == 0
    (shelf / "running" / catalog.RECIPIENTS).write_text(
        (shelf / "running" / catalog.RECIPIENTS).read_text().replace(colleague, f"{colleague}  # colleague")
    )
    monkeypatch.chdir(source)
    assert cli.main(["publish", "--catalog", str(shelf), "--revoke", colleague, "--revoke", identity]) == 0
    assert catalog.recipients(shelf, "running") == [identity]  # own key re-added, colleague gone with its comment
    assert "colleague" not in (shelf / "running" / catalog.RECIPIENTS).read_text()
    assert manifest(shelf)["version"] == 2
    assert catalog.recipients(shelf, "missing") == []


def test_entries_report_the_local_copy_and_a_broken_manifest(tmp_path: Path, shelf: Path) -> None:
    source = built(tmp_path / "running")
    assert publish(source, shelf) == 0
    (shelf / "broken").mkdir()
    (shelf / "broken" / catalog.MANIFEST).write_text("{")
    root = tmp_path / "kbs"
    broken, running = catalog.entries(shelf, root)
    assert (broken.name, broken.manifest, running.local) == ("broken", None, "")
    assert "broken/manifest.json" in str(broken.problem)
    assert not catalog.outdated(running) and not catalog.outdated(broken)
    assert pull(shelf, root / "running") == 0
    built(source, "v2")
    assert publish(source, shelf) == 0
    [_, running] = catalog.entries(shelf, root)
    assert (running.local, catalog.outdated(running)) == ("v1", True)


def test_location_prefers_the_environment_over_the_saved_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = tmp_path / "config" / "catalog"
    monkeypatch.delenv("KB_CATALOG", raising=False)
    assert catalog.location(saved) is None
    catalog.remember(tmp_path / "shelf", saved)
    assert catalog.location(saved) == (tmp_path / "shelf").resolve()
    monkeypatch.setenv("KB_CATALOG", "~/elsewhere")
    assert catalog.location(saved) == Path.home() / "elsewhere"
    monkeypatch.setenv("KB_CATALOG", "")  # empty is unset, not the current directory
    assert catalog.location(saved) == (tmp_path / "shelf").resolve()
    catalog.remember(None, saved)
    catalog.remember(None, saved)
    assert catalog.location(saved) is None


def test_connect_clones_once_and_refuses_bad_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[tuple[str, ...]] = []

    def gh(_cwd: Path, *args: str) -> str:
        calls.append(args)
        if args[1] == "create" and args[2] == "me/taken":
            raise catalog.CatalogError("gh repo failed: name already exists")
        (Path(args[3]) / ".git").mkdir(parents=True)
        return ""

    monkeypatch.setattr(catalog, "_gh", gh)
    saved = tmp_path / "saved"
    assert catalog.connect("not a repo", tmp_path / "c", saved=saved) == 1
    (tmp_path / "plain").mkdir()
    assert catalog.connect("me/kbs", tmp_path / "plain", saved=saved) == 1
    assert catalog.connect("me/taken", tmp_path / "t", create=True, saved=saved) == 1
    err = capsys.readouterr().err
    assert "use owner/name" in err and "is not a git clone" in err and "already exists" in err
    assert catalog.location(saved) is None and calls == [("repo", "create", "me/taken", "--private", "--add-readme",
                                                          "--description", "kb catalog")]  # fmt: skip
    calls.clear()
    assert catalog.connect("me/kbs", tmp_path / "c", saved=saved) == 0
    assert catalog.connect("me/kbs", tmp_path / "c", saved=saved) == 0  # an existing clone is only remembered
    assert calls == [("repo", "clone", "me/kbs", str((tmp_path / "c").resolve()))]
    assert catalog.location(saved) == (tmp_path / "c").resolve()


def test_private_knowledge_base_keeps_its_configuration_inside_the_bundle(tmp_path: Path, shelf: Path, capsys) -> None:
    source = built(tmp_path / "running")
    assert publish(source, shelf, private=True) == 0
    entry = shelf / "running"
    assert sorted(p.name for p in entry.iterdir()) == sorted(
        [catalog.MANIFEST, catalog.RECIPIENTS, manifest(shelf)["bundle"]["file"]]
    )
    built(source, "v2")
    assert publish(source, shelf) == 0  # stays private without --private
    assert manifest(shelf)["private"] is True
    assert catalog.show(shelf, tmp_path / "none") == 0
    assert capsys.readouterr().out.splitlines()[-1].endswith("(private)")

    target = tmp_path / "device" / "running"
    assert pull(shelf, target) == 0
    assert (target / "domain.yaml").read_text() == "name: running knowledge base\n"


def test_pull_refuses_a_bundle_that_does_not_match_its_manifest(tmp_path: Path, shelf: Path, capsys) -> None:
    assert publish(built(tmp_path / "running"), shelf) == 0
    packed = shelf / "running" / manifest(shelf)["bundle"]["file"]
    packed.write_bytes(packed.read_bytes()[:-10])  # an interrupted sync
    local = built(tmp_path / "device" / "running", "local")
    assert pull(shelf, local, force=True) == 1
    assert "does not match its manifest" in capsys.readouterr().err
    assert marker(local) == "local"


def test_publish_refuses_an_unbuilt_knowledge_base_a_bad_name_or_key_and_pull_an_unknown_name(
    tmp_path: Path, shelf: Path
) -> None:
    home = built(tmp_path / "running")
    (home / db.DEFAULT_PATH).unlink()
    assert publish(home, shelf) == 1
    assert catalog.publish(built(tmp_path / "other"), shelf, "Bad Name", [], None) == 1
    assert catalog.publish(built(tmp_path / "third"), shelf, "third", ["age1nope"], None) == 1
    assert list(shelf.iterdir()) == []
    assert pull(shelf, tmp_path / "device" / "running") == 1


def test_unpack_writes_only_configuration_paths(tmp_path: Path) -> None:
    with pytest.raises(bundle.BundleError, match="not a configuration file path"):
        bundle.write_files({"prompts/../../.ssh/config": "x"}, tmp_path, overwrite=True)
    with pytest.raises(bundle.BundleError, match="not a configuration file path"):
        bundle.write_files({"notes.md": "x"}, tmp_path, overwrite=True)
    (tmp_path / "domain.yaml").write_text("edited\n")
    written, differing = bundle.write_files({"domain.yaml": "stored\n", "eval/golden.yaml": "q\n"}, tmp_path, False)
    assert (written, differing) == (["eval/golden.yaml"], ["domain.yaml"])
    assert (tmp_path / "domain.yaml").read_text() == "edited\n"


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = t\n\temail = t@example.org\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607


@pytest.fixture
def releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stands in for gh: release assets live in a folder per tag."""
    store = tmp_path / "releases"

    def gh(_catalog: Path, *args: str) -> str:
        if args[:2] == ("repo", "view"):
            return "me/kb-catalog\n"
        if args[:2] == ("release", "create"):
            (store / args[2]).mkdir(parents=True)
            Path(args[3]).replace(store / args[2] / Path(args[3]).name)
            return ""
        if args[:2] == ("release", "download"):
            asset = args[args.index("--pattern") + 1]
            target = Path(args[args.index("--dir") + 1])
            (target / asset).write_bytes((store / args[2] / asset).read_bytes())
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(catalog, "_gh", gh)
    return store


@pytest.mark.usefixtures("git_env")
def test_git_catalog_uploads_the_bundle_as_a_release_and_commits_only_text(tmp_path: Path, releases: Path) -> None:
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote))
    laptop, desk = tmp_path / "laptop", tmp_path / "desk"
    git("clone", "-q", str(remote), str(laptop))
    (laptop / "README").write_text("catalog\n")
    git("-C", str(laptop), "add", "README")
    git("-C", str(laptop), "commit", "-qm", "init")
    git("-C", str(laptop), "push", "-q", "origin", "main")
    git("clone", "-q", str(remote), str(desk))

    source = built(tmp_path / "running")
    assert publish(source, laptop) == 0
    assert publish(source, laptop) == 0  # unchanged: no release, no commit
    assert git("-C", str(laptop), "status", "--porcelain") == ""
    assert git("-C", str(remote), "log", "--format=%s") == "Publish running v1\ninit\n"
    assert not any(
        name.endswith(".age") for name in git("-C", str(remote), "ls-tree", "-r", "--name-only", "main").split()
    )
    assert [p.name for p in releases.iterdir()] == ["running-v1"]
    assert manifest(laptop)["release"] == {"repo": "me/kb-catalog", "tag": "running-v1"}

    target = tmp_path / "device" / "running"
    assert pull(desk, target) == 0  # the second clone pulls the manifest first, then downloads the release
    assert marker(target) == "v1"


def test_cli_reads_the_catalog_from_the_environment_and_serves_a_bare_database(
    tmp_path: Path, shelf: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from kb import cli

    home = built(tmp_path / "running")
    (home / "domain.yaml").write_text(
        (Path(__file__).parents[1] / "examples" / "web-principles" / "domain.yaml").read_text()
    )
    monkeypatch.setenv("KB_CATALOG", str(shelf))
    monkeypatch.chdir(tmp_path)  # -C changes the working directory
    monkeypatch.setattr(setup, "KB_ROOT", tmp_path / "none")
    assert cli.main(["-C", str(home), "publish"]) == 0
    capsys.readouterr()
    assert cli.main(["catalog"]) == 0
    assert capsys.readouterr().out.startswith("running ")

    packed = shelf / "running" / manifest(shelf)["bundle"]["file"]
    alone = tmp_path / "alone.db"
    bundle.unpack(packed, alone, keys.load(keys.identity_path()))
    served = cli.serving_domain(tmp_path / "missing.yaml", alone)  # the database alone is a knowledge base
    assert served == cli.domain.load(home / "domain.yaml")


def test_keygen_never_replaces_an_identity(capsys, identity: str) -> None:
    from kb import cli

    before = keys.identity_path().read_text()
    assert cli.main(["keygen"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == identity
    assert keys.identity_path().read_text() == before
    assert keys.identity_path().stat().st_mode & 0o077 == 0
