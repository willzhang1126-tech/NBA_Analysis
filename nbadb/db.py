"""Database connection, tracked upserts (with revision history) and run logging."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DB_PATH = os.environ.get("NBA_DB_PATH", "nba.db")
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
TRACKED = {"games", "team_game_stats", "player_game_stats", "player_game_status"}  # keep revision history


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=None):
    con = sqlite3.connect(path or DB_PATH)
    con.execute("PRAGMA foreign_keys = OFF")
    # views hold no data: drop and recreate so schema edits apply to existing databases
    for (v,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        con.execute(f"DROP VIEW {v}")
    con.executescript(SCHEMA_FILE.read_text())
    return con


def _py(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, "item") else v


def upsert(con, table, df, key_cols, run_id=None, keep_cols=()):
    """Insert or update rows. For tracked tables, every changed value is written
    to `revisions` first, so old values can be reconstructed.
    keep_cols: columns never overwritten once set (e.g. finalized_at).
    Returns (written, changed)."""
    if df is None or df.empty:
        return 0, 0
    cols = list(df.columns)
    rows = [tuple(_py(v) for v in r) for r in df.itertuples(index=False)]
    changed = 0
    if table in TRACKED:
        ki = [cols.index(k) for k in key_cols]
        where = " AND ".join(f"{k} = ?" for k in key_cols)
        ts = now_utc()
        revs = []
        for r in rows:
            key = tuple(r[i] for i in ki)
            old = con.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}", key).fetchone()
            if old is None:
                continue
            diffs = [(c, o, n) for c, o, n in zip(cols, old, r)
                     if c not in key_cols and c not in keep_cols and c not in ("last_updated", "source")
                     and not _same(o, n)]
            if diffs:
                changed += 1
                revs += [(run_id, ts, table, "|".join(map(str, key)), c, _s(o), _s(n)) for c, o, n in diffs]
        if revs:
            con.executemany("INSERT INTO revisions VALUES (?,?,?,?,?,?,?)", revs)
    updates = [c for c in cols if c not in key_cols and c not in keep_cols]
    set_sql = ", ".join(f"{c}=excluded.{c}" for c in updates)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT ({', '.join(key_cols)}) DO " + (f"UPDATE SET {set_sql}" if updates else "NOTHING"))
    con.executemany(sql, rows)
    return len(rows), changed


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


def _s(v):
    return None if v is None else str(v)


class Run:
    """One command execution. Logs each source call to source_runs."""

    def __init__(self, con, command):
        self.con = con
        with con:
            cur = con.execute("INSERT INTO runs (command, started_at, status) VALUES (?,?,?)",
                              (command, now_utc(), "running"))
        self.id = cur.lastrowid
        self.errors = 0

    def log(self, source, detail="", status="ok", fetched=0, written=0, changed=0, error=None):
        if status != "ok":
            self.errors += 1
        with self.con:
            self.con.execute("INSERT INTO source_runs VALUES (?,?,?,?,?,?,?,?,?)",
                             (self.id, source, detail, status, fetched, written, changed,
                              None if error is None else str(error)[:500], now_utc()))

    def finish(self):
        with self.con:
            self.con.execute("UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?",
                             (now_utc(), "ok" if not self.errors else f"{self.errors} source errors", self.id))
