"""Gold layer: silver readings -> AQI and daily summaries, ready for the website and ML.

Run:  python -m aqi_india.gold

1. Model AQI per city and hour (Open-Meteo concentrations -> CPCB formula, in Python: aqi.py)
2. Station AQI per hour       (data.gov.in sub-indices -> max sub-index, in SQL;
                               OpenAQ concentrations -> the same Python formula as 1)
3. Daily summaries + daily pollutant stats (SQL)
Merging measured vs. modeled happens in the views from sql/003_gold_views.sql.

Everything is an upsert over a recent window, so gold can always be recomputed.
"""
from __future__ import annotations

import json
import logging

import pandas as pd
import psycopg

from .aggregates import hourly_aqi
from .config import TIMEZONE
from .pg import connect
from .silver import QUALITY_NAMES, _rows

log = logging.getLogger(__name__)

CODES = {name: code for code, name in QUALITY_NAMES.items()}       # "PM2.5" -> "pm25"
SI_COLS = [f"{code}_si" for code in QUALITY_NAMES]                  # pm25_si, pm10_si, ...
AQI_COLS = ["source", "location_id", "observed_at", "aqi", "category", "dominant", *SI_COLS]


def upsert_aqi(conn: psycopg.Connection, df: pd.DataFrame) -> int:
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in AQI_COLS[3:])
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO gold.aqi_hourly ({', '.join(AQI_COLS)}) VALUES ({', '.join(['%s'] * len(AQI_COLS))})"
            f" ON CONFLICT (source, location_id, observed_at) DO UPDATE SET {updates}, computed_at = now()",
            _rows(df, AQI_COLS),
        )
    return len(df)


def concentration_aqi(conn: psycopg.Connection, source: str, hours: int = 72) -> int:
    """AQI for the last `hours` from clean concentrations of one source:
    the Open-Meteo model (cities) or OpenAQ (CPCB stations, Q48).

    Reads 24 h MORE than it writes: the first hour written needs a full 24 h
    average behind it (the same reasoning as Q20). A new station therefore gets
    its first AQI after ~16 hours of readings (CPCB's minimum, aqi.py)."""
    cur = conn.execute(
        "SELECT location_id, observed_at, pollutant, value FROM silver.air_quality"
        " WHERE source = %s AND measure = 'concentration' AND flag = 'ok'"
        " AND observed_at >= now() - make_interval(hours => %s)", (source, hours + 24))
    readings = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    if readings.empty:
        return 0
    readings["observed_at"] = pd.to_datetime(readings["observed_at"], utc=True).dt.tz_convert(TIMEZONE)
    readings = readings.assign(source=source, location=readings["location_id"],
                               state=None, lat=None, lon=None,
                               pollutant=readings["pollutant"].map(QUALITY_NAMES))

    since = pd.Timestamp.now(tz=TIMEZONE) - pd.Timedelta(hours=hours)
    result = hourly_aqi(readings, since=since)          # the existing, tested CPCB calculation
    if result.empty:
        return 0
    subs = result["sub_indices"].map(json.loads)
    df = pd.DataFrame({
        "source": source,
        "location_id": result["location"],
        "observed_at": result["observed_at"],
        "aqi": result["aqi"],
        "category": result["category"],
        "dominant": result["dominant_pollutant"].map(CODES),
    })
    for name, code in CODES.items():
        df[f"{code}_si"] = subs.map(lambda s, n=name: s.get(n))
    return upsert_aqi(conn, df)


def model_aqi(conn: psycopg.Connection, hours: int = 72) -> int:
    """Model AQI per city (kept as a name: what concentration_aqi does for Open-Meteo)."""
    return concentration_aqi(conn, "open-meteo", hours)


STATION_AQI_SQL = """
INSERT INTO gold.aqi_hourly (source, location_id, observed_at, aqi, category, dominant,
                             pm25_si, pm10_si, no2_si, so2_si, co_si, o3_si, nh3_si)
SELECT 'cpcb', location_id, observed_at,
       max(value)::smallint,
       gold.aqi_category(max(value)::numeric),               -- float -> numeric isn't automatic
       (array_agg(pollutant ORDER BY value DESC))[1],          -- the pollutant with the highest sub-index
       max(value) FILTER (WHERE pollutant = 'pm25')::smallint,
       max(value) FILTER (WHERE pollutant = 'pm10')::smallint,
       max(value) FILTER (WHERE pollutant = 'no2')::smallint,
       max(value) FILTER (WHERE pollutant = 'so2')::smallint,
       max(value) FILTER (WHERE pollutant = 'co')::smallint,
       max(value) FILTER (WHERE pollutant = 'o3')::smallint,
       max(value) FILTER (WHERE pollutant = 'nh3')::smallint
FROM silver.air_quality
WHERE source = 'cpcb' AND measure = 'sub_index' AND flag = 'ok' AND value IS NOT NULL
  AND observed_at >= now() - make_interval(hours => %(hours)s)
GROUP BY location_id, observed_at
-- CPCB validity rule: at least 3 pollutants, one of them PM2.5 or PM10
HAVING count(*) >= 3 AND bool_or(pollutant IN ('pm25', 'pm10'))
ON CONFLICT (source, location_id, observed_at) DO UPDATE SET
    aqi = EXCLUDED.aqi, category = EXCLUDED.category, dominant = EXCLUDED.dominant,
    pm25_si = EXCLUDED.pm25_si, pm10_si = EXCLUDED.pm10_si, no2_si = EXCLUDED.no2_si,
    so2_si = EXCLUDED.so2_si, co_si = EXCLUDED.co_si, o3_si = EXCLUDED.o3_si,
    nh3_si = EXCLUDED.nh3_si, computed_at = now()
"""


def station_aqi(conn: psycopg.Connection, hours: int = 72) -> int:
    """CPCB already publishes sub-indices (Q40), so a station's AQI is just their maximum."""
    return conn.execute(STATION_AQI_SQL, {"hours": hours}).rowcount


DAILY_SUMMARY_SQL = """
WITH quality AS (                       -- share of a day's raw readings that passed the checks
    SELECT source, location_id, (observed_at AT TIME ZONE %(tz)s)::date AS day,
           100.0 * avg((flag = 'ok')::int) AS pct_ok
    FROM silver.air_quality
    WHERE (observed_at AT TIME ZONE %(tz)s)::date >= %(first_day)s
    GROUP BY 1, 2, 3
)
INSERT INTO gold.daily_summary (source, location_id, day, mean_aqi, min_aqi, max_aqi,
                                worst_category, hours_covered, pct_ok)
SELECT a.source, a.location_id, (a.observed_at AT TIME ZONE %(tz)s)::date AS day,
       round(avg(a.aqi), 1), min(a.aqi), max(a.aqi),
       gold.aqi_category(max(a.aqi)), count(*), max(q.pct_ok)
FROM gold.aqi_hourly a
LEFT JOIN quality q ON q.source = a.source AND q.location_id = a.location_id
                   AND q.day = (a.observed_at AT TIME ZONE %(tz)s)::date
WHERE (a.observed_at AT TIME ZONE %(tz)s)::date >= %(first_day)s
GROUP BY 1, 2, 3
ON CONFLICT (source, location_id, day) DO UPDATE SET
    mean_aqi = EXCLUDED.mean_aqi, min_aqi = EXCLUDED.min_aqi, max_aqi = EXCLUDED.max_aqi,
    worst_category = EXCLUDED.worst_category, hours_covered = EXCLUDED.hours_covered,
    pct_ok = EXCLUDED.pct_ok
"""

DAILY_POLLUTANT_SQL = """
INSERT INTO gold.daily_pollutant (source, location_id, day, pollutant,
                                  min_value, mean_value, max_value, n_readings)
SELECT source, location_id, (observed_at AT TIME ZONE %(tz)s)::date, pollutant,
       min(value), avg(value), max(value), count(*)
FROM silver.air_quality
-- concentrations only: averaging CPCB sub-indices into "pollutant levels" would mix units
WHERE measure = 'concentration' AND flag = 'ok' AND value IS NOT NULL
  AND (observed_at AT TIME ZONE %(tz)s)::date >= %(first_day)s
GROUP BY 1, 2, 3, 4
ON CONFLICT (source, location_id, day, pollutant) DO UPDATE SET
    min_value = EXCLUDED.min_value, mean_value = EXCLUDED.mean_value,
    max_value = EXCLUDED.max_value, n_readings = EXCLUDED.n_readings
"""


def daily(conn: psycopg.Connection, days: int = 3) -> tuple[int, int]:
    """Recompute the last `days` calendar days (India time): older days no longer change."""
    params = {"tz": TIMEZONE,
              "first_day": (pd.Timestamp.now(tz=TIMEZONE) - pd.Timedelta(days=days - 1)).date()}
    return (conn.execute(DAILY_SUMMARY_SQL, params).rowcount,
            conn.execute(DAILY_POLLUTANT_SQL, params).rowcount)


def run_gold() -> None:
    with connect() as conn:                 # one transaction: gold is never half-updated
        n_model = concentration_aqi(conn, "open-meteo")
        n_station = station_aqi(conn) + concentration_aqi(conn, "openaq")
        n_summary, n_pollutant = daily(conn)
        conn.commit()
    log.info("Gold: %d model AQI hours, %d station AQI hours, %d daily summaries, %d daily pollutant rows",
             n_model, n_station, n_summary, n_pollutant)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    run_gold()
