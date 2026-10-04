"""A knowledge base as one file: the published snapshot of its database, compressed and encrypted with age.

The snapshot keeps the source-of-truth tables and the extraction cache, so a pulled copy can take new sources
without paying again for the sections already extracted. It drops what `kb index` rebuilds, the FTS5 tables and
the vectors, and carries the configuration files verbatim (kb_files) plus facts about itself (kb_meta).
"""

import hashlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pyrage
import zstandard

from kb import db

CONFIG_FILES = ("domain.yaml", "sources.yaml")
CONFIG_DIRS = ("prompts", "eval")
FTS_TABLES = ("chunks_fts", "statements_fts")  # created by index.build_fts
LEVEL = 19  # zstd level: publishing is rare, the bundle is downloaded on every pull


class BundleError(Exception):
    pass


def config_files(home: Path) -> dict[str, str]:
    """Relative POSIX path -> text of the configuration files in home (dotfiles left out)."""
    paths = [home / name for name in CONFIG_FILES if (home / name).is_file()]
    for folder in CONFIG_DIRS:
        if (home / folder).is_dir():
            paths += sorted(p for p in (home / folder).rglob("*") if p.is_file() and not p.name.startswith("."))
    files = {}
    for path in paths:
        try:
            files[path.relative_to(home).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BundleError(f"{path}: {exc}") from exc
    return files


def snapshot(db_path: Path, files: dict[str, str], target: Path) -> str:
    """Write the publishable copy of db_path, with files as kb_files and kb_meta empty, to target.

    Returns the sha256 of target, which changes only when the content does (VACUUM lays pages out the same way).
    """
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        source.execute("VACUUM INTO ?", (str(target),))
    finally:
        source.close()
    conn = db.connect(target)  # also creates kb_files and kb_meta in a database built before they existed
    try:
        with conn:
            for table in FTS_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("DELETE FROM vectors")
            conn.execute("DELETE FROM kb_meta")
            conn.execute("DELETE FROM kb_files")
            conn.executemany("INSERT INTO kb_files (path, content) VALUES (?, ?)", sorted(files.items()))
        conn.execute("VACUUM")
    finally:
        conn.close()
    return sha256(target)


def stamp(path: Path, meta: dict[str, str]) -> None:
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO kb_meta (key, value) VALUES (?, ?)", sorted(meta.items()))
    finally:
        conn.close()


def pack(path: Path, out: Path, recipients: Sequence[pyrage.x25519.Recipient]) -> None:
    """Compress the database file at path and encrypt it to every recipient, into out."""
    with path.open("rb") as src, out.open("wb") as dst:
        pyrage.encrypt_io(zstandard.ZstdCompressor(level=LEVEL, threads=-1).stream_reader(src), dst, list(recipients))


def unpack(packed: Path, out: Path, identities: Sequence[pyrage.x25519.Identity]) -> None:
    """Decrypt and decompress a bundle into the database file out; raise BundleError when that fails."""
    try:
        with (
            packed.open("rb") as src,
            out.open("wb") as dst,
            zstandard.ZstdDecompressor().stream_writer(dst, closefd=False) as writer,
        ):
            pyrage.decrypt_io(src, writer, list(identities))
    except pyrage.DecryptError as exc:
        raise BundleError(f"cannot decrypt {packed.name}: {exc}; is your public key in its recipients?") from exc
    except zstandard.ZstdError as exc:
        raise BundleError(f"{packed.name} is not a knowledge base bundle: {exc}") from exc
    conn = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BundleError(f"{packed.name} holds a damaged database")
    except sqlite3.DatabaseError as exc:
        raise BundleError(f"{packed.name} does not hold a database: {exc}") from exc
    finally:
        conn.close()


def meta(path: Path) -> dict[str, str]:
    return dict(_rows(path, "SELECT key, value FROM kb_meta"))


def stored_files(path: Path) -> dict[str, str]:
    return dict(_rows(path, "SELECT path, content FROM kb_files"))


def write_files(files: dict[str, str], home: Path, overwrite: bool) -> tuple[list[str], list[str]]:
    """Write configuration files into home; returns (written, differing and left alone without overwrite).

    Only the configuration paths are accepted, so a crafted bundle cannot write anywhere else.
    """
    check_paths(files)
    written, differing = [], []
    for rel, text in sorted(files.items()):
        target = home / rel
        if target.exists() and target.read_text(encoding="utf-8") == text:
            continue
        if target.exists() and not overwrite:
            differing.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(rel)
    return written, differing


def check_paths(files: dict[str, str]) -> None:
    """Raise BundleError unless every path is a configuration file path inside the knowledge base directory."""
    for rel in files:
        parts = PurePosixPath(rel).parts
        allowed = rel in CONFIG_FILES or (len(parts) > 1 and parts[0] in CONFIG_DIRS)
        if not allowed or PurePosixPath(rel).is_absolute() or ".." in parts:
            raise BundleError(f"refusing to write {rel!r}: not a configuration file path")


def keep_derived(path: Path, previous: Path) -> int:
    """Copy vectors and extraction outputs of the database previous into the one at path; returns vectors kept.

    `kb index` then embeds only what is new or changed instead of the whole knowledge base.
    """
    if not previous.exists():
        return 0
    conn = db.connect(path)
    try:
        conn.execute("ATTACH DATABASE ? AS previous", (str(previous),))
        with conn:
            kept = conn.execute("INSERT OR IGNORE INTO vectors SELECT * FROM previous.vectors").rowcount
            conn.execute("INSERT OR IGNORE INTO extraction_cache SELECT * FROM previous.extraction_cache")
        return kept
    except sqlite3.Error:
        return 0  # a damaged or foreign previous database only costs embedding time
    finally:
        conn.close()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _rows(path: Path, query: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(query).fetchall()
    except sqlite3.OperationalError:  # a database without the table
        return []
    finally:
        conn.close()
