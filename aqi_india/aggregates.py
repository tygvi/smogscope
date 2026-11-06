"""Derived tables built from clean readings: hourly AQI history and daily summaries.

These are *views* of the raw data (design-qa.md, Q9): they can always be rebuilt,
so they are recomputed for a recent window on every run and upserted.
"""
from __future__ import annotations

import json

import pandas as pd

from aqi_india.aqi import category, compute_aqi, rolling_concentrations
from aqi_india.config import TIMEZONE

LOCATION_COLS = ["source", "location", "state", "lat", "lon"]
AQI_COLS = LOCATION_COLS + ["observed_at", "aqi", "category", "dominant_pollutant", "sub_indices"]


def hourly_aqi(readings: pd.DataFrame, since: pd.Timestamp | None = None) -> pd.DataFrame:
    """AQI for every hour of every location, from clean hourly readings.

    since: only return hours at or after this time. The pipeline passes
           (window start + 24 h), so every returned hour had a FULL 24 h of
           history inside the window (Q20).
    """
    rows = []
    for _, group in readings.groupby(["source", "location"]):
        hourly = group.pivot_table(index="observed_at", columns="pollutant", values="value")
        averaged = rolling_concentrations(hourly)
        if since is not None:
            averaged = averaged[averaged.index >= since]

        first = group.iloc[0]
        for observed_at, concentrations in averaged.iterrows():
            result = compute_aqi(concentrations.dropna().to_dict())
            if result is None:
                continue
            rows.append({
                **{c: first[c] for c in LOCATION_COLS},
                "observed_at": observed_at,
                "aqi": result.aqi,
                "category": result.category,
                "dominant_pollutant": result.dominant_pollutant,
                "sub_indices": json.dumps(result.sub_indices),
            })
    return pd.DataFrame(rows, columns=AQI_COLS)


def _local_date(ts: pd.Series) -> pd.Series:
    """Calendar date in India time, as an ISO string ("2026-11-06")."""
    return ts.dt.tz_convert(TIMEZONE).dt.strftime("%Y-%m-%d")


def daily_summary(aqi: pd.DataFrame, readings: pd.DataFrame) -> pd.DataFrame:
    """One row per (source, location, date): AQI statistics + data-quality statistics.

    aqi:      rows of the `aqi` table (any number per day)
    readings: assessed rows of `air_quality` (all flags, so quality can be measured)
    """
    if aqi.empty:
        return pd.DataFrame()
    aqi = aqi.assign(date=_local_date(aqi["observed_at"]))
    keys = ["source", "location", "date"]

    stats = aqi.groupby(keys).agg(
        state=("state", "first"),
        lat=("lat", "first"),
        lon=("lon", "first"),
        aqi_mean=("aqi", "mean"),
        aqi_min=("aqi", "min"),
        aqi_max=("aqi", "max"),
        aqi_hours=("aqi", "size"),
        # Most frequent dominant pollutant of the day (ties -> alphabetical first)
        dominant_pollutant=("dominant_pollutant", lambda s: s.mode().iloc[0]),
    )
    stats["aqi_mean"] = stats["aqi_mean"].round(1)

    # Worst category of the day comes from the day's highest AQI.
    stats["worst_category"] = stats["aqi_max"].map(category)

    if not readings.empty:
        r = readings.assign(date=_local_date(readings["observed_at"]))
        quality = r.groupby(keys).agg(
            readings_total=("flag", "size"),
            readings_ok=("flag", lambda f: (f == "ok").sum()),
        )
        quality["pct_ok"] = (100 * quality["readings_ok"] / quality["readings_total"]).round(1)
        stats = stats.join(quality, how="left")

    return stats.reset_index()


def daily_pollutants(readings: pd.DataFrame) -> pd.DataFrame:
    """One row per (source, location, date, pollutant): min / mean / max of CLEAN readings."""
    clean = readings[readings["flag"] == "ok"]
    if clean.empty:
        return pd.DataFrame()
    clean = clean.assign(date=_local_date(clean["observed_at"]))
    out = clean.groupby(["source", "location", "date", "pollutant"]).agg(
        unit=("unit", "first"),
        value_min=("value", "min"),
        value_mean=("value", "mean"),
        value_max=("value", "max"),
        n_readings=("value", "size"),
    )
    out["value_mean"] = out["value_mean"].round(2)
    return out.reset_index()
