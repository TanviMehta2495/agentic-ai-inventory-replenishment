import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
