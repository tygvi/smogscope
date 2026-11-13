"""SmogScope HTTP API (FastAPI), reading the PostgreSQL gold layer.

Run (development):  uvicorn aqi_india.api:app --reload
Interactive docs:   http://localhost:8000/docs

The API is READ-ONLY over the warehouse the scheduler fills. It never fetches
pollution data itself; the only outside call is the wind grid, which is cached.
If the frontend has been built (web/dist), it is served at "/" too, so the whole
app runs as one process in production.

Measured vs. estimated: a city's headline AQI comes from gold.city_best, which
uses the CPCB stations within 25 km when one reported in the last 2 hours, and
the Open-Meteo model otherwise. Every city says which one it is (`basis`).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated

import pandas as pd
import psycopg
import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel

from aqi_india.config import PROJECT_ROOT, STATION_FEED, TIMEZONE
from aqi_india.fetchers import open_meteo
from aqi_india.pg import PG_DSN
from aqi_india.silver import QUALITY_NAMES

log = logging.getLogger(__name__)

app = FastAPI(
    title="SmogScope API",
    version="0.2.0",
    description="Air quality across India: CPCB station measurements merged with modeled estimates, "
                "official CPCB AQI method, hourly history, daily summaries, wind.",
)

NAMES = QUALITY_NAMES                                  # "pm25" -> "PM2.5": the names the frontend uses
SI_COLS = [f"{code}_si" for code in NAMES]


# ---------------------------------------------------------------------------
# Database dependency: one connection per request, always closed afterwards.
# ---------------------------------------------------------------------------

def get_conn() -> Iterator[psycopg.Connection]:
    # autocommit: every query is read-only, so there's no transaction to keep open.
    # dict_row: rows come back as {"column": value} instead of tuples.
    conn = psycopg.connect(PG_DSN, autocommit=True, row_factory=dict_row)
    conn.execute(f"SET TIME ZONE '{TIMEZONE}'")        # timestamps come back in India time
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[psycopg.Connection, Depends(get_conn)]


# ---------------------------------------------------------------------------
# Response models: the API's contract with the frontend (web/src/api.ts).
# ---------------------------------------------------------------------------

class Weather(BaseModel):
    temperature_c: float | None
    humidity_pct: float | None
    wind_speed_kmh: float | None
    wind_dir_deg: float | None


class CityNow(BaseModel):
    location: str
    state: str | None
    lat: float
    lon: float
    observed_at: str
    aqi: int
    category: str
    basis: str                      # "measured" (CPCB stations) | "estimated" (model)
    dominant_pollutant: str | None
    sub_indices: dict[str, int]     # station average when measured, model when estimated
    stations: int | None            # stations behind a measured value
    min_aqi: int | None             # range across those stations
    max_aqi: int | None
    measured_aqi: int | None
    model_aqi: int | None
    model_gap: int | None           # model - measured: positive = model too high
    weather: Weather | None


class Station(BaseModel):
    id: int
    name: str
    city: str
    state: str
    lat: float
    lon: float
    observed_at: str | None
    aqi: int | None                 # None = too few pollutants for a valid AQI (CPCB rule)
    category: str | None
    dominant_pollutant: str | None
    sub_indices: dict[str, int]


class AqiPoint(BaseModel):
    t: str
    aqi: int
    category: str
    dominant_pollutant: str | None


class MeasuredPoint(BaseModel):
    t: str
    aqi: int
    min_aqi: int
    max_aqi: int
    stations: int


class ValuePoint(BaseModel):
    t: str
    value: float


class CityHistory(BaseModel):
    location: str
    hours: int
    aqi: list[AqiPoint]                 # model, hourly
    measured: list[MeasuredPoint]       # CPCB stations, whenever they reported
    pollutants: dict[str, list[ValuePoint]]
    units: dict[str, str]


class DailyRow(BaseModel):
    date: str
    aqi_mean: float | None
    aqi_min: int | None
    aqi_max: int | None
    aqi_hours: int | None
    worst_category: str | None
    dominant_pollutant: str | None
    pct_ok: float | None


class TimelineCity(BaseModel):
    location: str
    lat: float
    lon: float
    aqi: list[int | None]   # aligned with Timeline.times; None = no value that hour


class Timeline(BaseModel):
    times: list[str]
    cities: list[TimelineCity]


class Overview(BaseModel):
    observed_at: str | None
    cities: int
    measured: int                   # how many cities have a measured (station) value
    average_aqi: float | None
    counts_by_category: dict[str, int]
    worst: CityNow | None
    best: CityNow | None


class WindGrid(BaseModel):
    lat0: float
    lon0: float
    dlat: float
    dlon: float
    nx: int
    ny: int
    u: list[float]
    v: list[float]
    unit: str
    observed_at: str | None
    cached_at: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _int(x) -> int | None:
    return None if x is None else int(round(float(x)))


def _sub_indices(row: dict) -> dict[str, int]:
    """{pm25_si: 312, o3_si: None, ...} -> {"PM2.5": 312}"""
    return {NAMES[code]: _int(row[f"{code}_si"]) for code in NAMES if row.get(f"{code}_si") is not None}


def _dominant(subs: dict[str, int]) -> str | None:
    return max(subs, key=subs.get) if subs else None


def _city_id(conn: psycopg.Connection, location: str) -> int:
    row = conn.execute("SELECT location_id FROM silver.location WHERE kind = 'city' AND name = %s",
                       (location,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"No data for city {location!r}")
    return row["location_id"]


MODEL_SUBS_SQL = f"""
SELECT DISTINCT ON (location_id) location_id, observed_at, {', '.join(SI_COLS)}
FROM gold.aqi_hourly WHERE source = 'open-meteo'
ORDER BY location_id, observed_at DESC
"""

# Average sub-index per pollutant over a city's stations, for the recent hours.
MEASURED_SUBS_SQL = f"""
SELECT cs.city_id AS location_id, a.observed_at,
       {', '.join(f'avg(a.{c}) AS {c}' for c in SI_COLS)}
FROM gold.city_station cs
JOIN gold.aqi_hourly a ON a.location_id = cs.station_id AND a.source IN ('cpcb', 'openaq')
WHERE a.observed_at >= now() - interval '3 hours'
GROUP BY cs.city_id, a.observed_at
"""

LATEST_WEATHER_SQL = """
SELECT DISTINCT ON (location_id) location_id, temperature_c, humidity_pct, wind_speed_kmh, wind_dir_deg
FROM silver.weather ORDER BY location_id, observed_at DESC
"""


def _latest_cities(conn: psycopg.Connection) -> list[CityNow]:
    best = conn.execute("SELECT * FROM gold.city_best WHERE aqi IS NOT NULL").fetchall()
    model_subs = {r["location_id"]: r for r in conn.execute(MODEL_SUBS_SQL)}
    measured_subs = {(r["location_id"], r["observed_at"]): r for r in conn.execute(MEASURED_SUBS_SQL)}
    weather = {r["location_id"]: r for r in conn.execute(LATEST_WEATHER_SQL)}

    cities = []
    for b in best:
        loc = b["location_id"]
        if b["basis"] == "measured":
            subs_row = measured_subs.get((loc, b["observed_at"]), {})
        else:
            subs_row = model_subs.get(loc, {})
        subs = _sub_indices(subs_row) if subs_row else {}
        w = weather.get(loc)
        cities.append(CityNow(
            location=b["name"], state=b["state"], lat=b["lat"], lon=b["lon"],
            observed_at=_iso(b["observed_at"]), aqi=b["aqi"], category=b["category"], basis=b["basis"],
            dominant_pollutant=_dominant(subs), sub_indices=subs,
            stations=b["stations"], min_aqi=_int(b["min_aqi"]), max_aqi=_int(b["max_aqi"]),
            measured_aqi=b["measured_aqi"], model_aqi=b["model_aqi"], model_gap=b["model_gap"],
            weather=None if w is None else Weather(
                temperature_c=w["temperature_c"], humidity_pct=w["humidity_pct"],
                wind_speed_kmh=w["wind_speed_kmh"], wind_dir_deg=w["wind_dir_deg"],
            ),
        ))
    return sorted(cities, key=lambda c: c.aqi, reverse=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(conn: Conn) -> dict:
    """Freshness, not just "the process is up" (Q38): the newest AQI hour, and each source's last fetch."""
    latest = conn.execute("SELECT max(observed_at) AS t FROM gold.aqi_hourly").fetchone()["t"]
    runs = conn.execute(
        "SELECT DISTINCT ON (source) source, status, started_at, records FROM bronze.ingest_run"
        " ORDER BY source, run_id DESC").fetchall()
    # A deployment uses one station feed (Q48). Hide the other route's last run:
    # on the Sydney server CPCB is geo-blocked, but OpenAQ carries the same stations.
    active = {"open-meteo-aq", "open-meteo-weather",
              "openaq" if STATION_FEED == "openaq" else "cpcb"}
    runs = [r for r in runs if r["source"] in active]
    return {
        "status": "ok",
        "latest_aqi_at": _iso(latest),
        "sources": {r["source"]: {"status": r["status"], "at": _iso(r["started_at"]), "records": r["records"]}
                    for r in runs},
    }


@app.get("/api/cities", response_model=list[CityNow])
def cities(conn: Conn) -> list[CityNow]:
    """Latest AQI + weather per city, worst first: measured where stations exist, else estimated."""
    return _latest_cities(conn)


@app.get("/api/overview", response_model=Overview)
def overview(conn: Conn) -> Overview:
    """National snapshot: how many cities are in each category, worst and best."""
    now = _latest_cities(conn)
    counts: dict[str, int] = {}
    for c in now:
        counts[c.category] = counts.get(c.category, 0) + 1
    return Overview(
        observed_at=max((c.observed_at for c in now), default=None),
        cities=len(now),
        measured=sum(c.basis == "measured" for c in now),
        average_aqi=round(sum(c.aqi for c in now) / len(now), 1) if now else None,
        counts_by_category=counts,
        worst=now[0] if now else None,
        best=now[-1] if now else None,
    )


STATIONS_SQL = """
WITH latest AS (            -- data.gov.in: each station's most recent sub-index per pollutant (last 24 h)
    SELECT DISTINCT ON (location_id, pollutant) location_id, pollutant, value, observed_at
    FROM silver.air_quality
    WHERE source = 'cpcb' AND measure = 'sub_index' AND flag = 'ok' AND value IS NOT NULL
      AND observed_at >= now() - interval '24 hours'
    ORDER BY location_id, pollutant, observed_at DESC
), si AS (
    SELECT location_id, max(observed_at) AS observed_at,
           jsonb_object_agg(pollutant, round(value)) AS subs
    FROM latest GROUP BY location_id
), g AS (                   -- newest valid AQI from gold, from either station feed
    SELECT DISTINCT ON (location_id) location_id, observed_at, aqi, category, dominant,
           jsonb_strip_nulls(jsonb_build_object('pm25', pm25_si, 'pm10', pm10_si, 'no2', no2_si,
               'so2', so2_si, 'co', co_si, 'o3', o3_si, 'nh3', nh3_si)) AS subs
    FROM gold.aqi_hourly
    WHERE source IN ('cpcb', 'openaq') AND observed_at >= now() - interval '24 hours'
    ORDER BY location_id, observed_at DESC
), oaq AS (                 -- OpenAQ stations still collecting their first 16 hours (no AQI yet)
    SELECT DISTINCT location_id FROM silver.air_quality
    WHERE source = 'openaq' AND observed_at >= now() - interval '3 hours'
)
SELECT l.location_id AS id, l.name, l.city, l.state, l.lat, l.lon,
       coalesce(si.observed_at, g.observed_at) AS observed_at,
       coalesce(si.subs, g.subs, '{}'::jsonb) AS subs,
       g.aqi, g.category, g.dominant
FROM silver.location l
LEFT JOIN si  USING (location_id)
LEFT JOIN g   USING (location_id)
LEFT JOIN oaq USING (location_id)
WHERE l.kind = 'station'
  AND (si.location_id IS NOT NULL OR g.location_id IS NOT NULL OR oaq.location_id IS NOT NULL)
"""


@app.get("/api/stations", response_model=list[Station])
def stations(conn: Conn) -> list[Station]:
    """Every CPCB station that reported recently (via data.gov.in or OpenAQ), with its sub-indices and AQI."""
    out = []
    for r in conn.execute(STATIONS_SQL):
        subs = {NAMES.get(code, code): _int(v) for code, v in r["subs"].items()}
        out.append(Station(
            id=r["id"], name=r["name"], city=r["city"], state=r["state"], lat=r["lat"], lon=r["lon"],
            observed_at=_iso(r["observed_at"]), aqi=r["aqi"], category=r["category"],
            dominant_pollutant=NAMES.get(r["dominant"]) if r["dominant"] else None, sub_indices=subs,
        ))
    return out


@app.get("/api/cities/{location}/history", response_model=CityHistory)
def city_history(
    location: str,
    conn: Conn,
    hours: Annotated[int, Query(ge=1, le=24 * 30)] = 72,
) -> CityHistory:
    """Hourly model AQI, measured AQI whenever stations reported, and model concentrations (clean only)."""
    loc = _city_id(conn, location)
    window = {"loc": loc, "hours": hours}
    since = "observed_at >= now() - make_interval(hours => %(hours)s)"
    aqi = conn.execute(
        f"SELECT observed_at, aqi, category, dominant FROM gold.aqi_hourly"
        f" WHERE source = 'open-meteo' AND location_id = %(loc)s AND {since} ORDER BY observed_at", window)
    measured = conn.execute(
        f"SELECT observed_at, aqi, min_aqi, max_aqi, stations FROM gold.city_measured_hourly"
        f" WHERE location_id = %(loc)s AND {since} ORDER BY observed_at", window)
    readings = conn.execute(
        f"SELECT a.observed_at, a.pollutant, a.value, p.unit FROM silver.air_quality a"
        f" JOIN silver.pollutant p ON p.code = a.pollutant"
        f" WHERE a.source = 'open-meteo' AND a.measure = 'concentration' AND a.flag = 'ok'"
        f" AND a.location_id = %(loc)s AND a.{since} ORDER BY a.observed_at", window).fetchall()

    pollutants: dict[str, list[ValuePoint]] = {}
    units: dict[str, str] = {}
    for r in readings:
        name = NAMES[r["pollutant"]]
        pollutants.setdefault(name, []).append(ValuePoint(t=_iso(r["observed_at"]), value=round(r["value"], 3)))
        units[name] = r["unit"]
    return CityHistory(
        location=location,
        hours=hours,
        aqi=[AqiPoint(t=_iso(r["observed_at"]), aqi=r["aqi"], category=r["category"],
                      dominant_pollutant=NAMES.get(r["dominant"]) if r["dominant"] else None) for r in aqi],
        measured=[MeasuredPoint(t=_iso(r["observed_at"]), aqi=r["aqi"], min_aqi=r["min_aqi"],
                                max_aqi=r["max_aqi"], stations=r["stations"]) for r in measured],
        pollutants=pollutants,
        units=units,
    )


@app.get("/api/cities/{location}/daily", response_model=list[DailyRow])
def city_daily(
    location: str,
    conn: Conn,
    days: Annotated[int, Query(ge=1, le=366)] = 14,
) -> list[DailyRow]:
    """Daily summaries of the model AQI (complete hourly coverage), oldest first."""
    loc = _city_id(conn, location)
    rows = conn.execute(
        "SELECT day, mean_aqi, min_aqi, max_aqi, hours_covered, worst_category, pct_ok"
        " FROM gold.daily_summary WHERE source = 'open-meteo' AND location_id = %s"
        " ORDER BY day DESC LIMIT %s", (loc, days)).fetchall()
    return [DailyRow(
        date=r["day"].isoformat(),
        aqi_mean=None if r["mean_aqi"] is None else round(r["mean_aqi"], 1),
        aqi_min=r["min_aqi"], aqi_max=r["max_aqi"], aqi_hours=r["hours_covered"],
        worst_category=r["worst_category"], dominant_pollutant=None,
        pct_ok=None if r["pct_ok"] is None else round(r["pct_ok"], 1),
    ) for r in reversed(rows)]


@app.get("/api/timeline", response_model=Timeline)
def timeline(
    conn: Conn,
    hours: Annotated[int, Query(ge=1, le=24 * 7)] = 72,
) -> Timeline:
    """Hourly AQI of every city on ONE shared time axis, for map playback.

    Same rule as the live view: the measured value for an hour when stations
    reported, otherwise the model's. Shaped as parallel arrays (times[i] <-> aqi[i]):
    ~15 cities x 72 hours stays a few KB, and the frontend indexes by hour directly.
    """
    window = {"hours": hours}
    rows = conn.execute(
        """
        SELECT l.name AS location, l.lat, l.lon, a.observed_at, a.aqi, 1 AS priority
        FROM gold.aqi_hourly a JOIN silver.location l USING (location_id)
        WHERE a.source = 'open-meteo' AND l.kind = 'city'
          AND a.observed_at >= now() - make_interval(hours => %(hours)s)
        UNION ALL
        SELECT l.name, l.lat, l.lon, m.observed_at, m.aqi, 0
        FROM gold.city_measured_hourly m JOIN silver.location l USING (location_id)
        WHERE m.observed_at >= now() - make_interval(hours => %(hours)s)
        """, window).fetchall()
    if not rows:
        return Timeline(times=[], cities=[])
    df = pd.DataFrame(rows)
    df["t"] = df["observed_at"].map(_iso)
    # measured (priority 0) wins over model (priority 1) for the same city and hour
    df = df.sort_values("priority").drop_duplicates(["location", "t"], keep="first")
    times = sorted(df["t"].unique())
    wide = df.pivot_table(index="location", columns="t", values="aqi").reindex(columns=times)
    coords = df.groupby("location")[["lat", "lon"]].first()
    return Timeline(
        times=times,
        cities=[
            TimelineCity(
                location=loc, lat=coords.at[loc, "lat"], lon=coords.at[loc, "lon"],
                aqi=[None if pd.isna(v) else int(v) for v in wide.loc[loc]],
            )
            for loc in wide.index
        ],
    )


# ---- Wind: fetched from Open-Meteo on demand, cached in memory --------------

WIND_TTL_SECONDS = 60 * 60
_wind_cache: dict = {}
_wind_lock = threading.Lock()


def _fetch_wind() -> dict:
    return open_meteo.fetch_wind_grid()


@app.get("/api/wind", response_model=WindGrid)
def wind() -> WindGrid:
    """Current wind on a 2° grid over India (u/v in m/s), for the particle animation.

    Cached for an hour: the model updates hourly, and every grid point counts
    toward Open-Meteo's free daily quota. If a refresh fails, the last good grid
    is served (stale data beats no wind); with no cache at all -> 503.
    """
    with _wind_lock:  # one refresh at a time, even with many simultaneous visitors
        fresh = _wind_cache and time.time() - _wind_cache["cached_at"] < WIND_TTL_SECONDS
        if not fresh:
            try:
                _wind_cache.update(_fetch_wind(), cached_at=time.time())
            except (requests.RequestException, KeyError, ValueError):
                log.exception("Wind grid refresh failed")
                if not _wind_cache:
                    raise HTTPException(status_code=503, detail="Wind data temporarily unavailable")
        return WindGrid(**_wind_cache)


# ---- Frontend (production build) ------------------------------------------

_DIST = PROJECT_ROOT / "web" / "dist"
if _DIST.is_dir():
    # Mounted LAST so every /api route above takes precedence.
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
