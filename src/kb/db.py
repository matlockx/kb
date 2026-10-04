import sqlite3
from importlib.resources import files
from pathlib import Path

DEFAULT_PATH = Path("data/kb.db")
SCHEMA_VERSION = 1  # recorded in a published snapshot's kb_meta; pull refuses a newer one


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating its directory and schema if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(files("kb").joinpath("schema.sql").read_text(encoding="utf-8"))
    return conn
