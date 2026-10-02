"""A catalog of built knowledge bases to share between devices: `kb publish`, `kb catalog` and `kb pull`.

A catalog is a directory with one folder per knowledge base: its domain.yaml, sources.yaml, prompts/, eval/ and a
copy of data/kb.db; raw downloads stay behind. The directory can be synced by a cloud drive, or be a git clone,
in which case reading pulls first and publishing commits and pushes.
"""

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import yaml

from kb import setup
from kb.db import DEFAULT_PATH as DB
from kb.sources import ID_RE

CONFIG = ("domain.yaml", "sources.yaml", "prompts", "eval")  # copied as they are; data/kb.db is added on top


def publish(home: Path, catalog: Path, name: str) -> int:
    """Copy the knowledge base in home into catalog/name, replacing the previous copy; commit and push in a git clone.

    The database is copied with VACUUM INTO, a consistent snapshot even while another process reads it.
    """
    if not ID_RE.fullmatch(name):
        print(f"name {name!r} must be lowercase letters, digits and single hyphens; pass --name", file=sys.stderr)
        return 1
    missing = [rel for rel in (*CONFIG, str(DB)) if not (home / rel).exists()]
    if missing:
        print(f"{home} has no {', '.join(missing)}; build it first (setup NAME or kb index)", file=sys.stderr)
        return 1
    if not catalog.is_dir():
        print(f"catalog {catalog} is not a directory", file=sys.stderr)
        return 1
    _git_pull(catalog)
    stage = Path(tempfile.mkdtemp(dir=catalog, prefix=f".{name}-"))
    try:
        for rel in CONFIG:
            _copy(home / rel, stage / rel)
        (stage / DB).parent.mkdir(parents=True)
        conn = sqlite3.connect(f"file:{home / DB}?mode=ro", uri=True)
        try:
            conn.execute("VACUUM INTO ?", (str(stage / DB),))
        finally:
            conn.close()
        _replace(stage, catalog / name)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    size = (catalog / name / DB).stat().st_size
    print(f"published {name} to {catalog / name} ({size / 1e6:.1f} MB)")
    if not _is_git(catalog):
        return 0
    _git(catalog, "add", "-A", "--", name)
    if _git(catalog, "diff", "--cached", "--quiet", "--", name):
        print(f"{name} is unchanged in the catalog; nothing to commit")
        return 0
    committed = _git(catalog, "commit", "--quiet", "-m", f"Publish {name}", "--", name)
    if not (committed and _git(catalog, "push", "--quiet")):
        print(f"git commit or push failed in {catalog}; the copy is there, push it by hand", file=sys.stderr)
        return 1
    print(f"committed and pushed {name}")
    return 0


def show(catalog: Path, root: Path | None = None) -> int:
    """Print every knowledge base in the catalog with its size, name and whether root (default ~/kbs) holds a copy."""
    if not catalog.is_dir():
        print(f"catalog {catalog} is not a directory", file=sys.stderr)
        return 1
    _git_pull(catalog)
    built = (path.parents[len(DB.parts) - 1] for path in catalog.glob(f"*/{DB}"))
    entries = sorted(entry for entry in built if not entry.name.startswith("."))  # skips copies being published
    if not entries:
        print(f"no knowledge bases in {catalog}")
        return 0
    for entry in entries:
        local = (root or setup.KB_ROOT) / entry.name
        mark = "installed" if (local / DB).exists() else ""
        print(f"{entry.name:<32} {(entry / DB).stat().st_size / 1e6:>7.1f} MB  {mark:<9}  {_title(entry)}")
    return 0


def pull(
    name: str,
    catalog: Path,
    home: Path | None,
    force: bool,
    ask: setup.Ask,
    omp_config: Path = setup.OMP_MCP,
    warm: Callable[[], object] | None = None,
) -> int:
    """Copy catalog/name into home (default ~/kbs/NAME), cache the embedding model and offer to register the MCP server.

    An existing knowledge base is replaced only with force: its configuration and database are overwritten, raw/ and
    other files are kept.
    """
    source = catalog / name
    _git_pull(catalog)
    if not ID_RE.fullmatch(name) or not (source / DB).exists():
        print(f"{name!r} is not in the catalog {catalog}; `kb catalog` lists it", file=sys.stderr)
        return 1
    home = (home or setup.KB_ROOT / name).expanduser().resolve()
    if (home / "domain.yaml").exists() and not force:
        print(f"{home} already holds a knowledge base; --force replaces its config and database", file=sys.stderr)
        return 1
    home.mkdir(parents=True, exist_ok=True)
    for rel in (*CONFIG, str(DB)):
        stage = Path(tempfile.mkdtemp(dir=home, prefix=".pull-"))  # beside the target, so the rename stays local
        try:
            _copy(source / rel, stage / "item")
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            _replace(stage / "item", home / rel)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    print(f"pulled {name} into {home}")
    try:
        (warm or _load_model)()  # the MCP server loads the model offline, so it has to be in the cache before
    except Exception as exc:  # any download failure leaves the copy usable once the model is cached
        print(f"warning: the embedding model is not cached ({exc}); `kb -C {home} index` downloads it", file=sys.stderr)
    setup.register_omp(name, home, ask, omp_config)
    return 0


def _load_model() -> None:
    from kb import index  # sentence-transformers is slow to import

    index.load_model()


def _title(entry: Path) -> str:
    try:
        data = yaml.safe_load((entry / "domain.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    return str(data.get("name", "")) if isinstance(data, dict) else ""


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _replace(new: Path, target: Path) -> None:
    """Move new into place; an existing target is moved aside first and deleted only once new is in place."""
    if not target.exists():
        new.replace(target)
        return
    old = target.with_name(f".{target.name}.old")
    _remove(old)
    target.replace(old)
    new.replace(target)
    _remove(old)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _is_git(catalog: Path) -> bool:
    return (catalog / ".git").exists()


def _git_pull(catalog: Path) -> None:
    """Fast-forward a git catalog; a failure (offline, diverged) leaves the local copy in use and says so."""
    if _is_git(catalog) and not _git(catalog, "pull", "--ff-only", "--quiet"):
        print(f"warning: git pull failed in {catalog}; using the local copy", file=sys.stderr)


def _git(catalog: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(catalog), *args], check=False).returncode == 0  # noqa: S603, S607
