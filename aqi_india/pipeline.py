"""One run: fetch -> store raw -> quality-check -> hourly AQI -> daily summaries.

Run it:   python -m aqi_india.pipeline
"""
from __future__ import annotations

import json
import logging

import pandas as pd
import requests

from aqi_india import aggregates, db
from aqi_india.aqi import compute_aqi
from aqi_india.config import CITIES, DATA_GOV_API_KEY, TIMEZONE
from aqi_india.fetchers import cpcb, open_meteo
from aqi_india.quality import FLAG_OK, assess

log = logging.getLogger(__name__)

# How much history each run re-processes. Must comfortably exceed the stuck-sensor
# run (6 h) plus the AQI's 24 h averaging window.
QUALITY_WINDOW = pd.Timedelta(hours=72)
# Daily summaries are rebuilt for today and the previous N days (late data, re-flags).
DAILY_LOOKBACK_DAYS = 2


def aqi_from_station_averages(readings: pd.DataFrame) -> pd.DataFrame:
    """AQI per CPCB station. CPCB already publishes averaged values, so use the
    LATEST value of each pollutant directly (older snapshots are history, not inputs)."""
    rows = []
    for (source, _), group in readings.groupby(["source", "location"]):
        latest = group.sort_values("observed_at").groupby("pollutant").last()
        result = compute_aqi(latest["value"].to_dict())
        if result is None:
            continue
        first = group.iloc[0]
        rows.append({
            **{c: first[c] for c in aggregates.LOCATION_COLS},
            "observed_at": latest["observed_at"].max(),
            "aqi": result.aqi,
            "category": result.category,
            "dominant_pollutant": result.dominant_pollutant,
            "sub_indices": json.dumps(result.sub_indices),
        })
    return pd.DataFrame(rows, columns=aggregates.AQI_COLS)


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
    """Run one full cycle and return the AQI rows computed in this run."""
    conn = db.connect()
    try:
        _ingest(conn)
        now = pd.Timestamp.now(tz=TIMEZONE)
        window_start = now - QUALITY_WINDOW

        # 1. Quality: re-assess the recent window (stuck checks need history),
        #    write flags back, and keep only clean readings for everything below.
        recent = assess(db.read_since(conn, "air_quality", window_start))
        db.update_flags(conn, recent)
        log.info("Quality: %s", ", ".join(f"{k}={v}" for k, v in recent["flag"].value_counts().items()))
        clean = recent[recent["flag"] == FLAG_OK]

        # 2. AQI. Modeled hourly data -> an AQI for EVERY hour that had a full
        #    24 h of history inside the window. CPCB -> latest snapshot per station.
        aqi_table = pd.concat([
            aggregates.hourly_aqi(clean[clean["source"] == open_meteo.SOURCE],
                                  since=window_start + pd.Timedelta(hours=24)),
            aqi_from_station_averages(clean[clean["source"] == cpcb.SOURCE]),
        ], ignore_index=True)
        log.info("AQI: %d rows stored", db.upsert(conn, "aqi", aqi_table))

        # 3. Daily summaries for the last few calendar days (India time).
        day_start = now.normalize() - pd.Timedelta(days=DAILY_LOOKBACK_DAYS)
        days_aqi = db.read_since(conn, "aqi", day_start)
        days_readings = recent[recent["observed_at"] >= day_start]
        log.info("Daily summary: %d rows stored",
                 db.upsert(conn, "daily_summary", aggregates.daily_summary(days_aqi, days_readings)))
        log.info("Daily pollutants: %d rows stored",
                 db.upsert(conn, "daily_pollutants", aggregates.daily_pollutants(days_readings)))
        return aqi_table
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    table = run_once()
    if table.empty:
        print("No AQI values could be computed.")
        return
    latest = (
        table[table["source"] == open_meteo.SOURCE]
        .sort_values("observed_at")
        .groupby("location").last()
        .sort_values("aqi", ascending=False)
        .reset_index()
    )
    print("\nCurrent AQI (Open-Meteo modeled, CPCB method):\n")
    print(latest[["location", "aqi", "category", "dominant_pollutant"]].to_string(index=False))


if __name__ == "__main__":
    main()
