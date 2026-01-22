import sqlite3
from pathlib import Path

DB_PATH = Path("/data/events.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  title TEXT,
  start TEXT,
  end TEXT,
  type_id INTEGER,
  type_name TEXT,
  color TEXT,
  comment TEXT,
  descr TEXT,
  updated_at TEXT,
  user_id TEXT,
  user_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE INDEX IF NOT EXISTS idx_events_updated ON events(updated_at);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
"""

def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

def get_meta(key: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()

def set_meta(key: str, value: str):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()

def upsert_event(ev: dict):
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO events(id,title,start,end,type_id,type_name,color,comment,descr,updated_at,user_id,user_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,
              start=excluded.start,
              end=excluded.end,
              type_id=excluded.type_id,
              type_name=excluded.type_name,
              color=excluded.color,
              comment=excluded.comment,
              descr=excluded.descr,
              updated_at=excluded.updated_at,
              user_id=excluded.user_id,
              user_name=excluded.user_name
            """,
            (
                ev["id"],
                ev.get("title"),
                ev.get("start"),
                ev.get("end"),
                ev.get("type_id"),
                ev.get("type_name"),
                ev.get("color"),
                ev.get("comment"),
                ev.get("descr"),
                ev.get("updated_at"),
                ev.get("user_id"),
                ev.get("user_name"),
            ),
        )
        conn.commit()
    finally:
        conn.close()

def query_events(time_min_iso: str, time_max_iso: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE start < ? AND end > ?
              AND user_id != '1'
            ORDER BY start ASC

            """,
            (time_max_iso, time_min_iso),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
