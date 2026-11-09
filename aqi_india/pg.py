"""PostgreSQL warehouse: bronze layer writers (raw API responses, append-only).

Run once:  python -m aqi_india.pg
"""
import logging
import os
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

# Where the database is. Default: local "smogscope" DB as the current Mac user.
PG_DSN = os.environ.get("SMOGSCOPE_PG_DSN", "dbname=smogscope")


def connect() -> psycopg.Connection:
    return psycopg.connect(PG_DSN)


@contextmanager
def ingest_run(conn: psycopg.Connection, source: str):
    """Record one fetch in bronze.ingest_run: 'running' -> 'ok' or 'failed'."""
    run_id = conn.execute(
        "INSERT INTO bronze.ingest_run (source) VALUES (%s) RETURNING run_id", (source,)
    ).fetchone()[0]
    conn.commit()                       # the 'running' row is saved even if we crash later
    run = {"run_id": run_id, "records": 0}
    try:
        yield run
    except BaseException as e:          # BaseException, not Exception: also catches Ctrl+C
        conn.rollback()                 # throw away this run's half-written records...
        conn.execute(                   # ...but keep a record that it failed, and why
            "UPDATE bronze.ingest_run SET status = 'failed', finished_at = now(), error = %s"
            " WHERE run_id = %s", (repr(e)[:2000], run_id))
        conn.commit()
        raise
    else:
        conn.execute(
            "UPDATE bronze.ingest_run SET status = 'ok', finished_at = now(), records = %s"
            " WHERE run_id = %s", (run["records"], run_id))
        conn.commit()


def store_cpcb_records(conn: psycopg.Connection, run_id: int, records: list[dict]) -> int:
    """Insert each raw CPCB record, exactly as received, as one JSON row."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO bronze.cpcb_record (run_id, record) VALUES (%s, %s)",
            [(run_id, Jsonb(r)) for r in records],
        )
    return len(records)


def ingest_cpcb() -> dict:
    from .config import DATA_GOV_API_KEY
    from .fetchers.cpcb import _fetch_all_records

    with connect() as conn, ingest_run(conn, "cpcb") as run:
        records = _fetch_all_records(DATA_GOV_API_KEY, page_size=500, max_pages=20)
        run["records"] = store_cpcb_records(conn, run["run_id"], records)
    return run


# --- Open-Meteo ---------------------------------------------------------------
# Table names can't be passed as %s parameters (only values can), so they come from
# this fixed mapping, never from outside input. That rules out SQL injection.
OPEN_METEO_TABLES = {
    "open-meteo-aq": "bronze.open_meteo_air_quality",
    "open-meteo-weather": "bronze.open_meteo_weather",
}


def store_open_meteo(conn: psycopg.Connection, source: str, run_id: int, cities, results: list[dict]) -> int:
    """One row per city: the full JSON document Open-Meteo returned for it."""
    table = OPEN_METEO_TABLES[source]
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table} (run_id, location, payload) VALUES (%s, %s, %s)",
            [(run_id, city.name, Jsonb(result)) for city, result in zip(cities, results, strict=True)],
        )
    return len(results)


def ingest_open_meteo(source: str) -> dict:
    """Fetch the last 2 days, hourly, for all cities in ONE request, and keep it raw.

    Weather is now hourly history too (the SQLite pipeline only kept the 'current'
    snapshot, so hours missed while the laptop slept were lost for good).
    Forecast hours are kept in bronze; silver drops anything in the future.
    """
    from .config import CITIES
    from .fetchers import open_meteo as om

    url, variables = {
        "open-meteo-aq": (om.AIR_QUALITY_URL, om.AQ_VARIABLES),
        "open-meteo-weather": (om.WEATHER_URL, om.WEATHER_VARIABLES),
    }[source]
    with connect() as conn, ingest_run(conn, source) as run:
        results = om._get(url, CITIES, {"hourly": ",".join(variables), "past_days": 2, "forecast_days": 1})
        run["records"] = store_open_meteo(conn, source, run["run_id"], CITIES, results)
    return run


def ingest_all() -> None:
    """Each source on its own: one failing (e.g. CPCB) must not stop the others."""
    for source, ingest in [
        ("cpcb", ingest_cpcb),
        ("open-meteo-aq", lambda: ingest_open_meteo("open-meteo-aq")),
        ("open-meteo-weather", lambda: ingest_open_meteo("open-meteo-weather")),
    ]:
        try:
            result = ingest()
            log.info("%s -> bronze: run %s stored %s records", source, result["run_id"], result["records"])
        except Exception as e:          # already logged as 'failed' in bronze.ingest_run
            log.error("%s -> bronze FAILED: %s", source, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    ingest_all()
