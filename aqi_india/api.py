"""SmogScope HTTP API (FastAPI).

Run (development):  uvicorn aqi_india.api:app --reload
Interactive docs:   http://localhost:8000/docs

The API is READ-ONLY over the database the scheduler fills. It never fetches
pollution data itself; the only outside call is the wind grid, which is cached.
If the frontend has been built (web/dist), it is served at "/" too, so the whole
app runs as one process in production.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from typing import Annotated

import pandas as pd
import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aqi_india import db
from aqi_india.config import DB_PATH, PROJECT_ROOT, TIMEZONE
from aqi_india.fetchers import open_meteo

log = logging.getLogger(__name__)

app = FastAPI(
    title="SmogScope API",
    version="0.1.0",
    description="Real-time air quality for Indian cities: official CPCB AQI method, hourly history, daily summaries, wind.",
)

DEFAULT_SOURCE = open_meteo.SOURCE
Source = Annotated[str, Query(description="Data source: 'open-meteo' (modeled) or 'cpcb' (stations)")]


# ---------------------------------------------------------------------------
# Database dependency: one connection per request, always closed afterwards.
# ---------------------------------------------------------------------------

def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect(DB_PATH, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


# ---------------------------------------------------------------------------
# Response models: the API's contract with the frontend. FastAPI validates
# every response against these and publishes them in /docs.
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
    dominant_pollutant: str
    sub_indices: dict[str, int]
    weather: Weather | None


class AqiPoint(BaseModel):
    t: str
    aqi: int
    category: str
    dominant_pollutant: str


class ValuePoint(BaseModel):
    t: str
    value: float


class CityHistory(BaseModel):
    location: str
    hours: int
    aqi: list[AqiPoint]
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
# Queries
# ---------------------------------------------------------------------------

LATEST_AQI_SQL = """
SELECT a.* FROM aqi a
JOIN (SELECT location, MAX(observed_at) AS latest FROM aqi WHERE source = ? GROUP BY location) l
  ON a.location = l.location AND a.observed_at = l.latest
WHERE a.source = ?
"""

LATEST_WEATHER_SQL = """
SELECT w.* FROM weather w
JOIN (SELECT location, MAX(observed_at) AS latest FROM weather GROUP BY location) l
  ON w.location = l.location AND w.observed_at = l.latest
"""


def _since(hours: int) -> str:
    return (pd.Timestamp.now(tz=TIMEZONE) - pd.Timedelta(hours=hours)).isoformat()


def _latest_cities(conn: sqlite3.Connection, source: str) -> list[CityNow]:
    aqi = db.read_sql(conn, LATEST_AQI_SQL, (source, source))
    weather = db.read_sql(conn, LATEST_WEATHER_SQL).set_index("location")
    cities = []
    for row in aqi.itertuples(index=False):
        w = weather.loc[row.location] if row.location in weather.index else None
        cities.append(CityNow(
            location=row.location, state=row.state, lat=row.lat, lon=row.lon,
            observed_at=row.observed_at, aqi=row.aqi, category=row.category,
            dominant_pollutant=row.dominant_pollutant, sub_indices=json.loads(row.sub_indices),
            weather=None if w is None else Weather(
                temperature_c=w.temperature_c, humidity_pct=w.humidity_pct,
                wind_speed_kmh=w.wind_speed_kmh, wind_dir_deg=w.wind_dir_deg,
            ),
        ))
    return sorted(cities, key=lambda c: c.aqi, reverse=True)


def _require_location(conn: sqlite3.Connection, location: str, source: str) -> None:
    found = conn.execute("SELECT 1 FROM aqi WHERE location = ? AND source = ? LIMIT 1", (location, source)).fetchone()
    if not found:
        raise HTTPException(status_code=404, detail=f"No data for location {location!r} from source {source!r}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(conn: Conn) -> dict:
    latest = conn.execute("SELECT MAX(observed_at) FROM aqi").fetchone()[0]
    return {"status": "ok", "latest_aqi_at": latest}


@app.get("/api/cities", response_model=list[CityNow])
def cities(conn: Conn, source: Source = DEFAULT_SOURCE) -> list[CityNow]:
    """Latest AQI + weather for every location, worst first."""
    return _latest_cities(conn, source)


@app.get("/api/overview", response_model=Overview)
def overview(conn: Conn, source: Source = DEFAULT_SOURCE) -> Overview:
    """National snapshot: how many cities are in each category, worst and best."""
    now = _latest_cities(conn, source)
    counts: dict[str, int] = {}
    for c in now:
        counts[c.category] = counts.get(c.category, 0) + 1
    return Overview(
        observed_at=max((c.observed_at for c in now), default=None),
        cities=len(now),
        average_aqi=round(sum(c.aqi for c in now) / len(now), 1) if now else None,
        counts_by_category=counts,
        worst=now[0] if now else None,
        best=now[-1] if now else None,
    )


@app.get("/api/cities/{location}/history", response_model=CityHistory)
def city_history(
    location: str,
    conn: Conn,
    hours: Annotated[int, Query(ge=1, le=24 * 30)] = 72,
    source: Source = DEFAULT_SOURCE,
) -> CityHistory:
    """Hourly AQI and hourly pollutant concentrations (clean readings only)."""
    _require_location(conn, location, source)
    since = _since(hours)
    aqi = db.read_sql(
        conn,
        "SELECT observed_at, aqi, category, dominant_pollutant FROM aqi "
        "WHERE location = ? AND source = ? AND observed_at >= ? ORDER BY observed_at",
        (location, source, since),
    )
    readings = db.read_sql(
        conn,
        "SELECT observed_at, pollutant, value, unit FROM air_quality "
        "WHERE location = ? AND source = ? AND observed_at >= ? AND flag = 'ok' ORDER BY observed_at",
        (location, source, since),
    )
    return CityHistory(
        location=location,
        hours=hours,
        aqi=[AqiPoint(t=r.observed_at, aqi=r.aqi, category=r.category, dominant_pollutant=r.dominant_pollutant)
             for r in aqi.itertuples(index=False)],
        pollutants={
            p: [ValuePoint(t=r.observed_at, value=round(r.value, 3)) for r in g.itertuples(index=False)]
            for p, g in readings.groupby("pollutant")
        },
        units=readings.groupby("pollutant")["unit"].first().to_dict(),
    )


@app.get("/api/cities/{location}/daily", response_model=list[DailyRow])
def city_daily(
    location: str,
    conn: Conn,
    days: Annotated[int, Query(ge=1, le=366)] = 14,
    source: Source = DEFAULT_SOURCE,
) -> list[DailyRow]:
    """Daily summaries, oldest first."""
    _require_location(conn, location, source)
    rows = db.read_sql(
        conn,
        "SELECT * FROM daily_summary WHERE location = ? AND source = ? ORDER BY date DESC LIMIT ?",
        (location, source, days),
    )
    rows = rows.astype(object).where(rows.notna(), None)  # NaN -> None (JSON null)
    return [DailyRow(**r) for r in rows.iloc[::-1].to_dict("records")]


@app.get("/api/timeline", response_model=Timeline)
def timeline(
    conn: Conn,
    hours: Annotated[int, Query(ge=1, le=24 * 7)] = 72,
    source: Source = DEFAULT_SOURCE,
) -> Timeline:
    """Hourly AQI of every location on ONE shared time axis, for map playback.

    Shaped as parallel arrays (times[i] <-> aqi[i]) rather than a list of objects:
    ~15 cities x 72 hours stays a few KB, and the frontend indexes by hour directly.
    """
    df = db.read_sql(
        conn,
        "SELECT location, lat, lon, observed_at, aqi FROM aqi WHERE source = ? AND observed_at >= ?",
        (source, _since(hours)),
    )
    if df.empty:
        return Timeline(times=[], cities=[])
    times = sorted(df["observed_at"].unique())
    wide = df.pivot_table(index="location", columns="observed_at", values="aqi").reindex(columns=times)
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
