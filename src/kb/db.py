import sqlite3
from importlib.resources import files
from pathlib import Path

DEFAULT_PATH = Path("data/kb.db")
SCHEMA_VERSION = 3  # recorded in a published snapshot's kb_meta; pull refuses a newer one
# Columns added after schema 1, with their declarations; connect adds the missing ones to an older database.
ADDED_COLUMNS = {
    "documents": ("scope TEXT", "translation_of TEXT"),
    "statements": ("effective_from TEXT", "original_chunk_id TEXT"),
}


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating its directory and schema if needed and upgrading an older schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(files("kb").joinpath("schema.sql").read_text(encoding="utf-8"))
    for table, columns in ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    return conn


def upgrade(path: Path) -> None:
    """Add the columns and tables of a newer schema to the database at path; it is opened writable only when
    something is missing."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        current = "reviews" in tables and all(
            column.split()[0] in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for table, columns in ADDED_COLUMNS.items()
            for column in columns
        )
    finally:
        conn.close()
    if not current:
        connect(path).close()
