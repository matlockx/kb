"""A catalog of published knowledge bases to share between devices: `kb publish`, `kb catalog` and `kb pull`.

A catalog is a directory, synced by a cloud drive or a git clone, with one folder per knowledge base holding
manifest.json (version, checksums, where the bundle is), recipients.txt (the age public keys that can open it) and,
unless the knowledge base is private, a readable copy of its configuration. The bundle is the database snapshot of
bundle.py, compressed and encrypted to the recipients. A folder catalog keeps the bundle beside the manifest; a git
catalog uploads it as a GitHub release asset with gh, so the git history only grows by small text files.
"""

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from kb import bundle, db, keys, setup
from kb.db import DEFAULT_PATH as DB
from kb.sources import ID_RE

MANIFEST = "manifest.json"
RECIPIENTS = "recipients.txt"
FORMAT = 1  # of manifest.json; pull refuses a newer one
ASSET_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]+\.kb\.zst\.age")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")  # GitHub owner/name
RECIPIENTS_HEADER = "# age public keys (age1...) that can open this knowledge base, one per line\n"

Index = Callable[[Path], int]  # database path -> exit code; rebuilds the FTS tables and vectors (cli.run_index)


class CatalogError(Exception):
    pass


def publish(home: Path, catalog: Path, name: str, add: list[str], private: bool | None) -> int:
    """Publish the knowledge base in home as the next version of catalog/name; commit and push in a git clone.

    add lists public keys to append to its recipients; the publisher's own keys are always recipients. private
    None keeps the previous setting (public at first).
    """
    if not ID_RE.fullmatch(name):
        print(f"name {name!r} must be lowercase letters, digits and single hyphens; pass --name", file=sys.stderr)
        return 1
    missing = [rel for rel in ("domain.yaml", str(DB)) if not (home / rel).exists()]
    if missing:
        print(f"{home} has no {', '.join(missing)}; build it first (setup NAME or kb index)", file=sys.stderr)
        return 1
    if not catalog.is_dir():
        print(f"catalog {catalog} is not a directory", file=sys.stderr)
        return 1
    _git_pull(catalog)
    entry = catalog / name
    work = Path(tempfile.mkdtemp(prefix=f"kb-publish-{name}-"))  # the plaintext snapshot never enters the catalog
    stage = Path(tempfile.mkdtemp(dir=catalog, prefix=f".{name}-"))  # beside the entry, so the rename stays local
    try:
        old = _manifest(entry)
        own = keys.public_keys(keys.load(keys.identity_path()))
        listed, recipients = _recipients(entry, [*own, *add])
        files = bundle.config_files(home)
        private = bool(old and old.get("private")) if private is None else private
        snapshot = work / "kb.db"
        content = bundle.snapshot(home / DB, files, snapshot)
        unchanged = (content, recipients, private)
        if old and (old.get("content_sha256"), old.get("recipients"), old.get("private")) == unchanged:
            print(f"{name} v{old['version']} is unchanged; nothing to publish")
            return 0
        version = old["version"] + 1 if old else 1
        published_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        bundle.stamp(
            snapshot,
            {"schema": str(db.SCHEMA_VERSION), "name": name, "version": str(version), "published_at": published_at},
        )
        packed = work / f"{name}-v{version}.kb.zst.age"
        bundle.pack(snapshot, packed, keys.recipients(recipients))
        manifest = {
            "format": FORMAT,
            "name": name,
            "version": version,
            "published_at": published_at,
            "private": private,
            "title": None if private else _title(files.get("domain.yaml", "")),
            "counts": counts(snapshot),
            "content_sha256": content,
            "recipients": recipients,
            "bundle": {"file": packed.name, "sha256": bundle.sha256(packed), "size": packed.stat().st_size},
        }
        new = stage / "entry"
        new.mkdir()
        if _is_git(catalog):
            manifest["release"] = _upload(catalog, name, version, packed)
        else:
            shutil.move(packed, new / packed.name)
        (new / RECIPIENTS).write_text(listed, encoding="utf-8")
        (new / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not private:
            bundle.write_files(files, new, overwrite=True)
        _replace(new, entry)
    except (CatalogError, bundle.BundleError, keys.KeysError) as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    where = f"GitHub release {manifest['release']['tag']}" if "release" in manifest else str(entry)
    print(
        f"published {name} v{version} to {where} ({manifest['bundle']['size'] / 1e6:.1f} MB, encrypted to "
        f"{len(recipients)} recipient{'s' if len(recipients) != 1 else ''})"
    )
    if not _is_git(catalog):
        return 0
    _git(catalog, "add", "-A", "--", name)
    committed = _git(catalog, "commit", "--quiet", "-m", f"Publish {name} v{version}", "--", name)
    if not (committed and _git(catalog, "push", "--quiet")):
        print(
            f"git commit or push failed in {catalog}; the bundle is uploaded, push the catalog by hand", file=sys.stderr
        )
        return 1
    print(f"committed and pushed {name}")
    return 0


def show(catalog: Path, root: Path | None = None) -> int:
    """Print every knowledge base in the catalog: version, bundle size, the copy root (default ~/kbs) holds, title."""
    if not catalog.is_dir():
        print(f"catalog {catalog} is not a directory", file=sys.stderr)
        return 1
    entries = [catalog / name for name in names(catalog)]
    if not entries:
        print(f"no knowledge bases in {catalog}")
        return 0
    for entry in entries:
        try:
            manifest = _manifest(entry)
        except CatalogError as exc:
            print(f"{entry.name:<32} {exc}")
            continue
        if manifest is None:  # removed since the glob
            continue
        local = _local_version(entry.name, (root or setup.KB_ROOT) / entry.name / DB)
        if local.startswith("v") and local != f"v{manifest['version']}":
            local += " (older)"
        title = manifest.get("title") or "(private)"
        print(
            f"{entry.name:<32} v{manifest['version']:<4} {manifest['bundle']['size'] / 1e6:>7.1f} MB  "
            f"{local:<14} {title}"
        )
    return 0


def names(catalog: Path) -> list[str]:
    """The knowledge bases in the catalog, sorted; a git catalog is pulled first."""
    _git_pull(catalog)
    return sorted(p.parent.name for p in catalog.glob(f"*/{MANIFEST}") if not p.parent.name.startswith("."))


def pull(
    name: str,
    catalog: Path,
    home: Path | None,
    force: bool,
    ask: setup.Ask,
    index: Index,
    omp_config: Path = setup.OMP_MCP,
) -> int:
    """Install catalog/name into home (default ~/kbs/NAME), rebuild its search index and offer to register it.

    An existing knowledge base is replaced only with force: its configuration and database are overwritten, its
    vectors and extraction outputs reused, raw/ and other files kept.
    """
    _git_pull(catalog)
    try:
        manifest = _manifest(catalog / name) if ID_RE.fullmatch(name) else None
    except CatalogError as exc:
        print(exc, file=sys.stderr)
        return 1
    if manifest is None:
        print(f"{name!r} is not in the catalog {catalog}; `kb catalog` lists it", file=sys.stderr)
        return 1
    home = (home or setup.KB_ROOT / name).expanduser().resolve()
    if (home / "domain.yaml").exists() and not force:
        print(f"{home} already holds a knowledge base; --force replaces its config and database", file=sys.stderr)
        return 1
    home.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=home, prefix=".pull-"))  # beside the target, so the rename stays local
    try:
        identities = keys.load(keys.identity_path())
        packed = _fetch(catalog, catalog / name, manifest, stage)
        snapshot = stage / "kb.db"
        bundle.unpack(packed, snapshot, identities)
        meta = bundle.meta(snapshot)
        if (meta.get("name"), meta.get("version")) != (name, str(manifest["version"])):
            found = f"{meta.get('name')} v{meta.get('version')}"
            raise CatalogError(f"the bundle holds {found}, not {name} v{manifest['version']}")
        schema = meta.get("schema", "")
        if not schema.isdigit() or int(schema) > db.SCHEMA_VERSION:
            raise CatalogError(f"{name} v{manifest['version']} needs a newer engine; update it and pull again")
        files = bundle.stored_files(snapshot)
        bundle.check_paths(files)  # before anything local is replaced
        kept = bundle.keep_derived(snapshot, home / DB)
        (home / DB).parent.mkdir(parents=True, exist_ok=True)
        _replace(snapshot, home / DB)
        bundle.write_files(files, home, overwrite=True)
    except (CatalogError, bundle.BundleError, keys.KeysError) as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print(f"pulled {name} v{manifest['version']} into {home}" + (f"; {kept} vectors reused" if kept else ""))
    try:
        indexed = index(home / DB) == 0
    except Exception as exc:  # e.g. the embedding model download failed; the copy stays usable once indexed
        print(f"warning: {exc}", file=sys.stderr)
        indexed = False
    if not indexed:
        print(f"warning: the search index is not built; run `kb -C {home} index`", file=sys.stderr)
    setup.register_omp(name, home, ask, omp_config)
    return 0


def _manifest(entry: Path) -> dict | None:
    """The manifest of a catalog entry; None when there is none, CatalogError when it cannot be used."""
    path = entry / MANIFEST
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{path}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("format"), int):
        raise CatalogError(f"{path}: not a knowledge base manifest")
    if manifest["format"] > FORMAT:
        raise CatalogError(f"{path}: format {manifest['format']} needs a newer engine")
    packed = manifest.get("bundle") if isinstance(manifest.get("bundle"), dict) else {}
    release = manifest.get("release")
    valid = (
        manifest.get("name") == entry.name
        and isinstance(manifest.get("version"), int)
        and ASSET_RE.fullmatch(str(packed.get("file", "")))
        and isinstance(packed.get("sha256"), str)
        and isinstance(packed.get("size"), int)
        and (
            release is None
            or (
                isinstance(release, dict)
                and REPO_RE.fullmatch(str(release.get("repo", "")))
                and release.get("tag") == f"{manifest['name']}-v{manifest['version']}"
            )
        )
    )
    if not valid:
        raise CatalogError(f"{path}: name, version, bundle or release missing or invalid")
    return manifest


def _recipients(entry: Path, wanted: list[str]) -> tuple[str, list[str]]:
    """(text of recipients.txt with wanted keys appended, the keys it lists)."""
    path = entry / RECIPIENTS
    text = path.read_text(encoding="utf-8") if path.exists() else RECIPIENTS_HEADER
    listed = keys.parse_recipients(text, str(path))
    for key in keys.parse_recipients("\n".join(wanted), "--recipient"):
        if key not in listed:
            text += ("" if text.endswith("\n") else "\n") + f"{key}\n"
            listed.append(key)
    return text, listed


def _upload(catalog: Path, name: str, version: int, packed: Path) -> dict[str, str]:
    repo = _gh(catalog, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
    tag = f"{name}-v{version}"
    notes = f"Encrypted knowledge base bundle; `kb pull {name}` from a clone of this catalog opens it."
    title = f"{name} v{version}"
    _gh(catalog, "release", "create", tag, str(packed), "--repo", repo, "--title", title, "--notes", notes)
    return {"repo": repo, "tag": tag}


def _fetch(catalog: Path, entry: Path, manifest: dict, stage: Path) -> Path:
    """The bundle of manifest as a local file, its checksum verified."""
    asset = manifest["bundle"]["file"]
    release = manifest.get("release")
    if isinstance(release, dict):
        _gh(
            catalog, "release", "download", str(release["tag"]), "--repo", str(release["repo"]),
            "--pattern", asset, "--dir", str(stage),
        )  # fmt: skip
        path = stage / asset
    else:
        path = entry / asset
    if not path.is_file():
        raise CatalogError(f"bundle {asset} is missing from {path.parent}")
    if bundle.sha256(path) != manifest["bundle"]["sha256"]:
        raise CatalogError(f"bundle {asset} does not match its manifest (incomplete download or replaced file)")
    return path


def _gh(catalog: Path, *args: str) -> str:
    try:
        done = subprocess.run(["gh", *args], cwd=catalog, capture_output=True, text=True, check=False)  # noqa: S603, S607
    except FileNotFoundError as exc:
        raise CatalogError(
            "a git catalog keeps bundles as GitHub release assets: install gh and run `gh auth login`"
        ) from exc
    if done.returncode != 0:
        raise CatalogError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()}")
    return done.stdout


def counts(path: Path) -> dict[str, int]:
    """Rows in documents and statements of the database at path, opened read-only."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("documents", "statements")}  # noqa: S608
    finally:
        conn.close()


def _local_version(name: str, path: Path) -> str:
    """'vN' for a pulled copy of name, 'built here' for another database, '' without one."""
    if not path.exists():
        return ""
    found = bundle.meta(path)
    return f"v{found['version']}" if found.get("name") == name and "version" in found else "built here"


def _title(domain_text: str) -> str:
    try:
        data = yaml.safe_load(domain_text)
    except yaml.YAMLError:
        return ""
    return str(data.get("name", "")) if isinstance(data, dict) else ""


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
