"""One ingestion run: fetch -> store raw -> compute AQI -> store AQI.

Run it:   python -m aqi_india.pipeline
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from aqi_india import db
from aqi_india.aqi import compute_aqi, concentrations_from_hourly
from aqi_india.config import CITIES, DATA_GOV_API_KEY
from aqi_india.fetchers import cpcb, open_meteo

log = logging.getLogger(__name__)


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
    for _, group in readings.groupby(["source", "location"]):
        hourly = group.pivot_table(index="observed_at", columns="pollutant", values="value")
        row = _aqi_row(group["source"].iloc[0], group, hourly.index.max(), concentrations_from_hourly(hourly))
        if row:
            rows.append(row)
    return pd.DataFrame(rows)


def aqi_from_station_averages(readings: pd.DataFrame) -> pd.DataFrame:
    """AQI per CPCB station. CPCB already publishes averaged values, so use them directly."""
    rows = []
    for _, group in readings.groupby(["source", "location"]):
        concentrations = group.groupby("pollutant")["value"].mean().to_dict()
        row = _aqi_row(group["source"].iloc[0], group, group["observed_at"].max(), concentrations)
        if row:
            rows.append(row)
    return pd.DataFrame(rows)


def run_once() -> pd.DataFrame:
    """Run one full ingestion cycle and return the freshly computed AQI table."""
    conn = db.connect()
    aqi_frames = []

    try:
        # --- Open-Meteo: modeled data, every city, no key needed ---
        aq = open_meteo.fetch_air_quality(CITIES)
        log.info("Open-Meteo air quality: %d rows stored", db.upsert(conn, "air_quality", aq))
        log.info("Open-Meteo weather: %d rows stored",
                 db.upsert(conn, "weather", open_meteo.fetch_weather(CITIES)))
        aqi_frames.append(aqi_from_hourly(aq))

        # --- CPCB: official station data, needs a data.gov.in key ---
        if DATA_GOV_API_KEY:
            try:
                station = cpcb.fetch_cpcb(DATA_GOV_API_KEY)
                log.info("CPCB: %d rows stored", db.upsert(conn, "air_quality", station))
                aqi_frames.append(aqi_from_station_averages(station))
            except Exception:
                # One source failing shouldn't kill the whole run.
                log.exception("CPCB fetch failed; continuing with Open-Meteo only")
        else:
            log.warning("DATA_GOV_API_KEY not set; skipping CPCB (see .env.example)")

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
