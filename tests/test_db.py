"""Tests for aqi_india/db.py, using a throwaway database in a temp folder."""
import pandas as pd

from aqi_india import db

TZ = "Asia/Kolkata"


def _aqi_frame(observed_at):
    return pd.DataFrame({
        "source": ["open-meteo"], "location": ["Delhi"], "state": ["Delhi"], "lat": [28.6], "lon": [77.2],
        "observed_at": observed_at, "aqi": [300], "category": ["Poor"],
        "dominant_pollutant": ["PM2.5"], "sub_indices": ['{"PM2.5": 300}'],
    })


def test_upsert_is_idempotent_and_updates(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    t = pd.Series([pd.Timestamp("2026-11-01 10:00", tz=TZ)])
    db.upsert(conn, "aqi", _aqi_frame(t))
    db.upsert(conn, "aqi", _aqi_frame(t).assign(aqi=310))   # same key -> update, not duplicate
    rows = db.read_sql(conn, "SELECT aqi, observed_at FROM aqi")
    assert rows["aqi"].tolist() == [310]
    assert rows["observed_at"].iloc[0] == "2026-11-01T10:00:00+05:30"


def test_upsert_handles_timestamps_in_object_column(tmp_path):
    # Regression: concatenating with an empty frame turns the datetime column into
    # dtype=object holding Timestamps, which sqlite3 can't bind.
    conn = db.connect(tmp_path / "t.db")
    frame = pd.concat([_aqi_frame(pd.Series([pd.Timestamp("2026-11-01 10:00", tz=TZ)])),
                       pd.DataFrame(columns=_aqi_frame([None]).columns)], ignore_index=True)
    assert frame["observed_at"].dtype == object
    assert db.upsert(conn, "aqi", frame) == 1


def test_read_since_round_trips_timestamps(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    times = pd.Series(pd.date_range("2026-11-01 00:00", periods=3, freq="h", tz=TZ))
    for t in times:
        db.upsert(conn, "aqi", _aqi_frame(pd.Series([t])))
    out = db.read_since(conn, "aqi", times[1])
    assert out["observed_at"].tolist() == times[1:].tolist()


def test_migration_adds_flag_columns_to_old_table(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE air_quality (source TEXT, location TEXT, state TEXT, lat REAL, lon REAL, "
                "observed_at TEXT, pollutant TEXT, value REAL, unit TEXT, fetched_at TEXT, "
                "PRIMARY KEY (source, location, observed_at, pollutant))")
    old.close()
    conn = db.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(air_quality)")}
    assert {"flag", "flag_reason"} <= columns
