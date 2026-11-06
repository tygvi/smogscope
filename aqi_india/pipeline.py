"""One ingestion run: fetch -> store raw -> quality-check -> compute AQI -> store AQI.

Run it:   python -m aqi_india.pipeline
"""
from __future__ import annotations

import json
import logging

import pandas as pd
import requests

from aqi_india import db
from aqi_india.aqi import compute_aqi, concentrations_from_hourly
from aqi_india.config import CITIES, DATA_GOV_API_KEY
from aqi_india.fetchers import cpcb, open_meteo
from aqi_india.quality import FLAG_OK, assess

log = logging.getLogger(__name__)

# How much history the quality checks look at. Must comfortably exceed both the
# stuck-sensor run (6 h) and the AQI's 24 h averaging window.
QUALITY_WINDOW = pd.Timedelta(hours=72)


def _aqi_row(source: str, group: pd.DataFrame, observed_at, concentrations: dict) -> dict | None:
    result = compute_aqi(concentrations)
    if result is None:
        return None
    first = group.iloc[0]
    return {
        "source": source,
        "location": first["location"],
        "state": first["state"],
        "lat": first["lat"],
        "lon": first["lon"],
        "observed_at": observed_at,
        "aqi": result.aqi,
        "category": result.category,
        "dominant_pollutant": result.dominant_pollutant,
        "sub_indices": json.dumps(result.sub_indices),
    }


def aqi_from_hourly(readings: pd.DataFrame) -> pd.DataFrame:
    """AQI per location from hourly readings (we do the 24 h / 8 h averaging ourselves)."""
    rows = []
    for (source, _), group in readings.groupby(["source", "location"]):
        hourly = group.pivot_table(index="observed_at", columns="pollutant", values="value")
        row = _aqi_row(source, group, hourly.index.max(), concentrations_from_hourly(hourly))
        if row:
            rows.append(row)
    return pd.DataFrame(rows)


def aqi_from_station_averages(readings: pd.DataFrame) -> pd.DataFrame:
    """AQI per CPCB station. CPCB already publishes averaged values, so use the
    LATEST value of each pollutant directly (older snapshots are history, not inputs)."""
    rows = []
    for (source, _), group in readings.groupby(["source", "location"]):
        latest = group.sort_values("observed_at").groupby("pollutant").last()
        row = _aqi_row(source, group, latest["observed_at"].max(), latest["value"].to_dict())
        if row:
            rows.append(row)
    return pd.DataFrame(rows)


def _ingest(conn) -> None:
    """Fetch every source and store raw readings. A failing source is skipped."""
    aq = open_meteo.fetch_air_quality(CITIES)
    log.info("Open-Meteo air quality: %d rows stored", db.upsert(conn, "air_quality", aq))
    log.info("Open-Meteo weather: %d rows stored",
             db.upsert(conn, "weather", open_meteo.fetch_weather(CITIES)))

    if not DATA_GOV_API_KEY:
        log.warning("DATA_GOV_API_KEY not set; skipping CPCB (see .env.example)")
        return
    try:
        station = cpcb.fetch_cpcb(DATA_GOV_API_KEY)
        log.info("CPCB: %d rows stored", db.upsert(conn, "air_quality", station))
    except (requests.RequestException, cpcb.CPCBResponseError):
        # Expected, temporary failures (server down, timeout, garbage response):
        # log and carry on with the other source. Anything else is a bug -> crash.
        log.exception("CPCB fetch failed; continuing without it")


def run_once() -> pd.DataFrame:
    """Run one full cycle and return the freshly computed AQI table."""
    conn = db.connect()
    try:
        _ingest(conn)

        # Quality: re-assess the recent window (stuck checks need history),
        # write flags back, and keep only clean readings for the AQI.
        since = pd.Timestamp.now(tz="Asia/Kolkata") - QUALITY_WINDOW
        recent = assess(db.read_air_quality_since(conn, since))
        db.update_flags(conn, recent)
        counts = recent["flag"].value_counts().to_dict()
        log.info("Quality: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
        clean = recent[recent["flag"] == FLAG_OK]

        aqi_frames = [
            aqi_from_hourly(clean[clean["source"] == open_meteo.SOURCE]),
            aqi_from_station_averages(clean[clean["source"] == cpcb.SOURCE]),
        ]
        aqi_table = pd.concat([f for f in aqi_frames if not f.empty], ignore_index=True)
        log.info("AQI: %d rows stored", db.upsert(conn, "aqi", aqi_table))
        return aqi_table
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    table = run_once()
    if table.empty:
        print("No AQI values could be computed.")
        return
    summary = (
        table[table["source"] == open_meteo.SOURCE]
        .sort_values("aqi", ascending=False)
        [["location", "aqi", "category", "dominant_pollutant"]]
    )
    print("\nCurrent AQI (Open-Meteo modeled, CPCB method):\n")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
