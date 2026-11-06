"""SQLite storage. Plain SQL on purpose: learn the SQL first, add an ORM later if you want.

Air-quality readings are stored in *long* format (one row per pollutant reading)
so adding a new pollutant never needs a schema change. This layout also moves
straight over to PostgreSQL + TimescaleDB in Phase 4.

Every table has a primary key on its natural identity, and writes are UPSERTs,
so re-running the pipeline never creates duplicates.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from aqi_india.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS air_quality (
    source      TEXT NOT NULL,
    location    TEXT NOT NULL,
    state       TEXT,
    lat         REAL,
    lon         REAL,
    observed_at TEXT NOT NULL,      -- ISO-8601 with +05:30 offset
    pollutant   TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    flag        TEXT NOT NULL DEFAULT 'ok',   -- ok | suspect | invalid (quality.py)
    flag_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source, location, observed_at, pollutant)
);

CREATE TABLE IF NOT EXISTS weather (
    source           TEXT NOT NULL,
    location         TEXT NOT NULL,
    state            TEXT,
    lat              REAL,
    lon              REAL,
    observed_at      TEXT NOT NULL,
    temperature_c    REAL,
    humidity_pct     REAL,
    wind_speed_kmh   REAL,
    wind_dir_deg     REAL,
    pressure_hpa     REAL,
    precipitation_mm REAL,
    cloud_cover_pct  REAL,
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (source, location, observed_at)
);

CREATE TABLE IF NOT EXISTS aqi (
    source             TEXT NOT NULL,
    location           TEXT NOT NULL,
    state              TEXT,
    lat                REAL,
    lon                REAL,
    observed_at        TEXT NOT NULL,
    aqi                INTEGER NOT NULL,
    category           TEXT NOT NULL,
    dominant_pollutant TEXT NOT NULL,
    sub_indices        TEXT NOT NULL,  -- JSON, e.g. {"PM2.5": 182, "NO2": 51}
    fetched_at         TEXT NOT NULL,
    PRIMARY KEY (source, location, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_aq_time  ON air_quality (observed_at);
CREATE INDEX IF NOT EXISTS idx_aqi_time ON aqi (observed_at);
"""

PRIMARY_KEYS = {
    "air_quality": ["source", "location", "observed_at", "pollutant"],
    "weather": ["source", "location", "observed_at"],
    "aqi": ["source", "location", "observed_at"],
}


# Columns added after a table was first created. CREATE TABLE IF NOT EXISTS does
# nothing for a table that already exists, so older databases need these added.
MIGRATIONS = {
    "air_quality": {
        "flag": "TEXT NOT NULL DEFAULT 'ok'",
        "flag_reason": "TEXT NOT NULL DEFAULT ''",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.commit()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _to_sql_values(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].map(lambda t: t.isoformat())
    # object dtype turns numpy ints/floats into plain Python values; NaN -> None (SQL NULL)
    return df.astype(object).where(df.notna(), None)


def upsert(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """Insert rows, overwriting any existing row with the same primary key."""
    if df.empty:
        return 0
    df = df.assign(fetched_at=pd.Timestamp.now(tz="UTC").isoformat())
    df = _to_sql_values(df)

    cols = list(df.columns)
    keys = PRIMARY_KEYS[table]
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
    )
    with conn:  # one transaction: all rows or none
        conn.executemany(sql, df.itertuples(index=False, name=None))
    return len(df)


def read_sql(conn: sqlite3.Connection, query: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=params)


def read_air_quality_since(conn: sqlite3.Connection, since: pd.Timestamp) -> pd.DataFrame:
    """All air-quality readings observed at or after `since`, with tz-aware timestamps.

    Comparing ISO strings works because every stored timestamp has the same
    format and +05:30 offset (see design-qa.md, Q8 gotcha); `since` is converted
    to that same form first.
    """
    since_str = since.tz_convert("Asia/Kolkata").isoformat()
    df = read_sql(conn, "SELECT * FROM air_quality WHERE observed_at >= ?", (since_str,))
    df["observed_at"] = pd.to_datetime(df["observed_at"], format="ISO8601").dt.tz_convert("Asia/Kolkata")
    return df


def update_flags(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Write quality flags back onto existing air_quality rows."""
    if df.empty:
        return 0
    rows = (
        (r.flag, r.flag_reason, r.source, r.location, r.observed_at.isoformat(), r.pollutant)
        for r in df.itertuples(index=False)
    )
    with conn:
        conn.executemany(
            "UPDATE air_quality SET flag = ?, flag_reason = ? "
            "WHERE source = ? AND location = ? AND observed_at = ? AND pollutant = ?",
            rows,
        )
    return len(df)
