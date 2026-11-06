"""Tests for aqi_india/aggregates.py and the rolling AQI helper."""
import json

import pandas as pd
import pytest

from aqi_india.aggregates import daily_pollutants, daily_summary, hourly_aqi
from aqi_india.aqi import concentrations_from_hourly, rolling_concentrations

TZ = "Asia/Kolkata"


def make_long(values_by_pollutant, hours=30, start="2026-11-01 00:00", location="Delhi"):
    """Hourly long-format readings, same number of hours for each pollutant."""
    times = pd.date_range(start, periods=hours, freq="h", tz=TZ)
    frames = []
    for pollutant, values in values_by_pollutant.items():
        frames.append(pd.DataFrame({
            "source": "open-meteo", "location": location, "state": "X", "lat": 0.0, "lon": 0.0,
            "observed_at": times, "pollutant": pollutant, "value": values,
            "unit": "mg/m³" if pollutant == "CO" else "µg/m³", "flag": "ok",
        }))
    return pd.concat(frames, ignore_index=True)


# ---- rolling_concentrations ----

def test_rolling_last_row_matches_latest_concentrations():
    idx = pd.date_range("2026-11-01", periods=30, freq="h", tz=TZ)
    hourly = pd.DataFrame({"PM2.5": range(30), "O3": [10] * 22 + [100] * 8}, index=idx)
    rolled = rolling_concentrations(hourly)
    assert rolled.iloc[-1].to_dict() == pytest.approx(concentrations_from_hourly(hourly))


def test_rolling_needs_16_hours_before_first_value():
    idx = pd.date_range("2026-11-01", periods=20, freq="h", tz=TZ)
    rolled = rolling_concentrations(pd.DataFrame({"PM2.5": [50] * 20}, index=idx))
    assert rolled["PM2.5"].iloc[:15].isna().all()   # hours 1-15: not enough data
    assert rolled["PM2.5"].iloc[15:].notna().all()  # from the 16th hour on


# ---- hourly_aqi ----

def test_hourly_aqi_one_row_per_valid_hour():
    readings = make_long({"PM2.5": [75] * 30, "PM10": [80] * 30, "NO2": [30] * 30})
    out = hourly_aqi(readings)
    assert len(out) == 30 - 15            # first 15 hours lack 16 h of history
    assert (out["aqi"] == 151).all()      # constant air -> constant AQI (see test_aqi.py)
    assert json.loads(out["sub_indices"].iloc[0])["PM2.5"] == 151


def test_hourly_aqi_since_filters_hours():
    readings = make_long({"PM2.5": [75] * 30, "PM10": [80] * 30, "NO2": [30] * 30})
    since = pd.Timestamp("2026-11-02 00:00", tz=TZ)
    out = hourly_aqi(readings, since=since)
    assert out["observed_at"].min() == since
    assert len(out) == 6                  # 00:00-05:00 on Nov 2


def test_hourly_aqi_skips_invalid_combinations():
    # Only 2 pollutants -> CPCB validity rule fails -> no rows
    assert hourly_aqi(make_long({"PM2.5": [75] * 30, "NO2": [30] * 30})).empty


def test_hourly_aqi_separates_locations():
    a = make_long({"PM2.5": [75] * 20, "PM10": [80] * 20, "NO2": [30] * 20}, hours=20, location="A")
    b = make_long({"PM2.5": [20] * 20, "PM10": [30] * 20, "NO2": [10] * 20}, hours=20, location="B")
    out = hourly_aqi(pd.concat([a, b], ignore_index=True))
    assert set(out.groupby("location")["aqi"].first()) == {151, 33}


# ---- daily_summary / daily_pollutants ----

def _aqi_rows(location, times, values, dominant):
    return pd.DataFrame({
        "source": "open-meteo", "location": location, "state": "X", "lat": 0.0, "lon": 0.0,
        "observed_at": pd.to_datetime(times).tz_localize(TZ), "aqi": values,
        "dominant_pollutant": dominant,
    })


def test_daily_summary_stats_and_worst_category():
    aqi = _aqi_rows("Delhi", ["2026-11-01 10:00", "2026-11-01 11:00", "2026-11-01 12:00"],
                    [150, 250, 310], ["PM2.5", "PM2.5", "PM10"])
    out = daily_summary(aqi, pd.DataFrame()).iloc[0]
    assert out["date"] == "2026-11-01"
    assert (out["aqi_min"], out["aqi_max"], out["aqi_hours"]) == (150, 310, 3)
    assert out["aqi_mean"] == pytest.approx(236.7)
    assert out["worst_category"] == "Very Poor"
    assert out["dominant_pollutant"] == "PM2.5"   # most frequent, not the worst hour's


def test_daily_summary_uses_india_calendar_date():
    # 20:00 UTC on Nov 1 is 01:30 IST on Nov 2
    aqi = _aqi_rows("Delhi", ["2026-11-02 01:30"], [100], ["PM2.5"])
    assert daily_summary(aqi, pd.DataFrame()).iloc[0]["date"] == "2026-11-02"


def test_daily_summary_quality_percentage():
    aqi = _aqi_rows("Delhi", ["2026-11-01 10:00"], [100], ["PM2.5"])
    readings = make_long({"PM2.5": [50] * 4}, hours=4)
    readings.loc[0, "flag"] = "suspect"
    out = daily_summary(aqi, readings).iloc[0]
    assert (out["readings_total"], out["readings_ok"], out["pct_ok"]) == (4, 3, 75.0)


def test_daily_pollutants_excludes_flagged_readings():
    readings = make_long({"PM2.5": [10, 20, 30, 999]}, hours=4)
    readings.loc[3, "flag"] = "invalid"
    out = daily_pollutants(readings).iloc[0]
    assert (out["value_min"], out["value_mean"], out["value_max"], out["n_readings"]) == (10, 20, 30, 3)
