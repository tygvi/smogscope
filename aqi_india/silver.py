"""Silver layer: bronze raw JSON -> clean, typed, de-duplicated rows.

Run:  python -m aqi_india.silver

For every bronze run that succeeded and hasn't been transformed yet:
  CPCB records         -> silver.location (stations) + silver.air_quality (measure='sub_index')
  Open-Meteo AQ JSON   -> silver.location (cities)   + silver.air_quality (measure='concentration')
  Open-Meteo weather   -> silver.weather (one row per city x hour)
Then the quality checks re-flag the last 72 h of concentrations.

Properties: incremental (bronze.ingest_run.silver_done_at), idempotent (upserts),
one transaction per run (all of it or none), lineage (run_id on every row).
"""
from __future__ import annotations

import logging

import pandas as pd
import psycopg

from . import quality
from .config import CITIES, TIMEZONE
from .fetchers import open_meteo as om
from .pg import connect

log = logging.getLogger(__name__)

# CPCB pollutant_id -> our code
CPCB_POLLUTANTS = {
    "PM2.5": "pm25", "PM10": "pm10", "NO2": "no2", "SO2": "so2",
    "CO": "co", "OZONE": "o3", "NH3": "nh3",
}
# Open-Meteo variable -> (our code, multiplier into our unit)
OM_POLLUTANTS = {
    "pm2_5": ("pm25", 1.0), "pm10": ("pm10", 1.0), "nitrogen_dioxide": ("no2", 1.0),
    "sulphur_dioxide": ("so2", 1.0), "ozone": ("o3", 1.0),
    "carbon_monoxide": ("co", 0.001),       # µg/m³ -> mg/m³ (the CPCB unit)
}
# our code -> the names quality.py's thresholds are keyed by
QUALITY_NAMES = {"pm25": "PM2.5", "pm10": "PM10", "no2": "NO2", "so2": "SO2", "co": "CO", "o3": "O3", "nh3": "NH3"}

AQ_COLS = ["source", "location_id", "pollutant", "measure", "observed_at", "value", "flag", "flag_reason", "run_id"]
WEATHER_COLS = list(om.WEATHER_VARIABLES.values())


# --- helpers -------------------------------------------------------------------

def _rows(df: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """DataFrame -> plain Python tuples for psycopg: NaN/NaT -> None (SQL NULL),
    numpy numbers -> Python numbers. (psycopg would store float NaN as 'NaN', not NULL.)"""
    sub = df[cols].astype(object)
    return list(sub.where(sub.notna(), None).itertuples(index=False, name=None))


def upsert_locations(conn: psycopg.Connection, rows: list[tuple]) -> dict[tuple[str, str], int]:
    """rows: (kind, name, city, state, lat, lon). New places are inserted, existing ones
    left alone, so location_id never changes. Returns {(kind, name): location_id}."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO silver.location (kind, name, city, state, lat, lon)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (kind, name) DO NOTHING",
            rows,
        )
    kinds = sorted({r[0] for r in rows})
    found = conn.execute(
        "SELECT kind, name, location_id FROM silver.location WHERE kind = ANY(%s)", (kinds,)
    ).fetchall()
    return {(kind, name): loc_id for kind, name, loc_id in found}


def city_ids(conn: psycopg.Connection) -> dict[str, int]:
    ids = upsert_locations(conn, [("city", c.name, c.name, c.state, c.lat, c.lon) for c in CITIES])
    return {name: loc_id for (kind, name), loc_id in ids.items() if kind == "city"}


def upsert_air_quality(conn: psycopg.Connection, df: pd.DataFrame) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO silver.air_quality (source, location_id, pollutant, measure, observed_at,"
            " value, flag, flag_reason, run_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s::silver.quality_flag, %s, %s)"
            " ON CONFLICT (source, location_id, pollutant, measure, observed_at) DO UPDATE SET"
            " value = EXCLUDED.value, flag = EXCLUDED.flag,"
            " flag_reason = EXCLUDED.flag_reason, run_id = EXCLUDED.run_id",
            _rows(df, AQ_COLS),
        )
    return len(df)


def _hourly_frame(payload: dict, now: pd.Timestamp) -> pd.DataFrame:
    """Open-Meteo's parallel arrays {time: [...], var: [...]} -> one row per hour, past only."""
    hourly = pd.DataFrame(payload["hourly"])
    hourly["observed_at"] = pd.to_datetime(hourly.pop("time")).dt.tz_localize(TIMEZONE)
    return hourly[hourly["observed_at"] <= now]            # forecasts are not observations


# --- transforms (one per bronze source) -----------------------------------------

def transform_cpcb(conn: psycopg.Connection, run_id: int) -> int:
    records = [r for (r,) in conn.execute("SELECT record FROM bronze.cpcb_record WHERE run_id = %s", (run_id,))]
    df = pd.DataFrame(records)
    if df.empty:
        return 0

    df["lat"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["lon"] = pd.to_numeric(df["longitude"], errors="coerce")
    no_coords = ~(df["lat"].between(-90, 90) & df["lon"].between(-180, 180))   # NaN -> rejected too
    if no_coords.any():
        log.warning("CPCB run %s: skipped %d records without valid coordinates", run_id, no_coords.sum())
    df = df[~no_coords]

    stations = df.drop_duplicates("station")
    ids = upsert_locations(conn, [("station", s.station, s.city, s.state, s.lat, s.lon)
                                  for s in stations.itertuples()])

    df["pollutant"] = df["pollutant_id"].map(CPCB_POLLUTANTS)
    unknown = df["pollutant"].isna()
    if unknown.any():
        log.warning("CPCB run %s: skipped unknown pollutants %s",
                    run_id, df.loc[unknown, "pollutant_id"].value_counts().to_dict())
    df = df[~unknown].copy()

    df["value"] = pd.to_numeric(df["avg_value"], errors="coerce")          # "NA" -> NaN -> NULL
    df["observed_at"] = pd.to_datetime(df["last_update"], format="%d-%m-%Y %H:%M:%S",
                                       errors="coerce").dt.tz_localize(TIMEZONE)
    df = df.dropna(subset=["observed_at"])
    df["location_id"] = [ids[("station", s)] for s in df["station"]]

    # These are sub-indices (Q40): the only check that makes sense is the 0-500 scale.
    bad = df["value"].notna() & ~df["value"].between(0, 500)
    df["flag"] = bad.map({True: quality.FLAG_INVALID, False: quality.FLAG_OK})
    df["flag_reason"] = bad.map({True: "out_of_range", False: None})
    df = df.assign(source="cpcb", measure="sub_index", run_id=run_id)
    return upsert_air_quality(conn, df)


def transform_open_meteo_aq(conn: psycopg.Connection, run_id: int) -> int:
    ids = city_ids(conn)
    now = pd.Timestamp.now(tz=TIMEZONE)
    frames = []
    for location, payload in conn.execute(
        "SELECT location, payload FROM bronze.open_meteo_air_quality WHERE run_id = %s", (run_id,)
    ):
        hourly = _hourly_frame(payload, now)
        long = hourly.melt(id_vars="observed_at", var_name="variable", value_name="value")
        long = long[long["variable"].isin(OM_POLLUTANTS.keys())].dropna(subset=["value"])
        mapping = long["variable"].map(OM_POLLUTANTS)
        long["pollutant"] = mapping.str[0]
        long["value"] = long["value"] * mapping.str[1]
        long["location_id"] = ids[location]
        frames.append(long)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True).assign(
        source="open-meteo", measure="concentration", flag=quality.FLAG_OK, flag_reason=None, run_id=run_id)
    return upsert_air_quality(conn, df)       # real flags are set by reflag_concentrations()


def transform_open_meteo_weather(conn: psycopg.Connection, run_id: int) -> int:
    ids = city_ids(conn)
    now = pd.Timestamp.now(tz=TIMEZONE)
    frames = []
    for location, payload in conn.execute(
        "SELECT location, payload FROM bronze.open_meteo_weather WHERE run_id = %s", (run_id,)
    ):
        hourly = _hourly_frame(payload, now).rename(columns=om.WEATHER_VARIABLES)
        hourly["location_id"] = ids[location]
        frames.append(hourly)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True).assign(source="open-meteo", run_id=run_id)
    for col in WEATHER_COLS:                   # a variable Open-Meteo didn't send -> NULL
        if col not in df:
            df[col] = None

    cols = ["source", "location_id", "observed_at", *WEATHER_COLS, "run_id"]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in [*WEATHER_COLS, "run_id"])
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO silver.weather ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
            f" ON CONFLICT (source, location_id, observed_at) DO UPDATE SET {updates}",
            _rows(df, cols),
        )
    return len(df)


TRANSFORMS = {
    "cpcb": transform_cpcb,
    "open-meteo-aq": transform_open_meteo_aq,
    "open-meteo-weather": transform_open_meteo_weather,
}


# --- quality flags ------------------------------------------------------------

def reflag_concentrations(conn: psycopg.Connection, hours: int = 72) -> dict[str, int]:
    """Re-run quality.assess() over the recent window. A window, not just the new rows,
    because "stuck for 6 hours" can only be seen with the earlier hours alongside."""
    cur = conn.execute(
        "SELECT source, location_id, pollutant, measure, observed_at, value FROM silver.air_quality"
        " WHERE measure = 'concentration' AND value IS NOT NULL"
        " AND observed_at >= now() - make_interval(hours => %s)", (hours,))
    df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    if df.empty:
        return {}
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True)
    checked = quality.assess(df.assign(location=df["location_id"],
                                       pollutant=df["pollutant"].map(QUALITY_NAMES)))
    df["flag"] = checked["flag"]
    df["flag_reason"] = checked["flag_reason"].replace("", None)

    with conn.cursor() as c:
        c.executemany(
            "UPDATE silver.air_quality SET flag = %s::silver.quality_flag, flag_reason = %s"
            " WHERE source = %s AND location_id = %s AND pollutant = %s AND measure = %s AND observed_at = %s",
            _rows(df, ["flag", "flag_reason", "source", "location_id", "pollutant", "measure", "observed_at"]),
        )
    return df["flag"].value_counts().to_dict()


# --- entry point ----------------------------------------------------------------

def run_silver() -> None:
    with connect() as conn:
        pending = conn.execute(
            "SELECT run_id, source FROM bronze.ingest_run"
            " WHERE status = 'ok' AND silver_done_at IS NULL ORDER BY run_id"   # oldest first: newest wins
        ).fetchall()
        if not pending:
            log.info("Silver: nothing new in bronze")
        for run_id, source in pending:
            try:
                n = TRANSFORMS[source](conn, run_id)
                conn.execute("UPDATE bronze.ingest_run SET silver_done_at = now() WHERE run_id = %s", (run_id,))
                conn.commit()                                   # this run: all or nothing
                log.info("Silver: run %s (%s) -> %d rows", run_id, source, n)
            except Exception:
                conn.rollback()
                log.exception("Silver: run %s (%s) FAILED; will retry next time", run_id, source)
        flags = reflag_concentrations(conn)
        conn.commit()
        log.info("Silver: quality flags over last 72 h: %s", flags)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    run_silver()
