import numpy as np
import pandas as pd
import pytest

from aqi_india.aqi import category, compute_aqi, concentrations_from_hourly, sub_index


# ---- sub_index: breakpoints and interpolation ----

@pytest.mark.parametrize(
    "pollutant, conc, expected",
    [
        ("PM2.5", 0, 0),
        ("PM2.5", 30, 50),      # top of "Good"
        ("PM2.5", 60, 100),     # top of "Satisfactory"
        ("PM2.5", 75, 150.5),   # midway through 60-90 -> midway through 101-200
        ("PM2.5", 250, 400),
        ("PM10", 100, 100),
        ("PM10", 250, 200),
        ("CO", 2.0, 100),       # mg/m³
        ("O3", 100, 100),
        ("NO2", 180, 200),
    ],
)
def test_sub_index_breakpoints(pollutant, conc, expected):
    assert sub_index(pollutant, conc) == pytest.approx(expected)


def test_sub_index_caps_at_500():
    assert sub_index("PM2.5", 10_000) == 500


def test_sub_index_jumps_into_next_band():
    # Just above a breakpoint lands at the bottom of the next band (51), not 50.x
    assert sub_index("PM2.5", 30.0001) == pytest.approx(51, abs=0.01)


@pytest.mark.parametrize("bad", [None, np.nan, -5])
def test_sub_index_missing_or_invalid(bad):
    assert sub_index("PM2.5", bad) is None


def test_sub_index_unknown_pollutant():
    with pytest.raises(ValueError):
        sub_index("XYZ", 10)


# ---- category ----

@pytest.mark.parametrize(
    "aqi, name",
    [(0, "Good"), (50, "Good"), (51, "Satisfactory"), (150, "Moderate"),
     (300, "Poor"), (301, "Very Poor"), (450, "Severe")],
)
def test_category(aqi, name):
    assert category(aqi) == name


# ---- compute_aqi: max rule + validity rule ----

def test_compute_aqi_takes_max_sub_index():
    result = compute_aqi({"PM2.5": 75, "PM10": 80, "NO2": 30})
    assert result is not None
    assert result.dominant_pollutant == "PM2.5"
    assert result.aqi == 151  # 150.5 rounds half-up
    assert result.category == "Moderate"
    assert set(result.sub_indices) == {"PM2.5", "PM10", "NO2"}


def test_compute_aqi_needs_three_pollutants():
    assert compute_aqi({"PM2.5": 75, "NO2": 30}) is None


def test_compute_aqi_needs_a_particulate():
    assert compute_aqi({"NO2": 30, "SO2": 10, "O3": 40}) is None


def test_compute_aqi_ignores_nan_and_unknown():
    assert compute_aqi({"PM2.5": 75, "NO2": np.nan, "SO2": 10, "dust": 5}) is None


# ---- concentrations_from_hourly: averaging windows ----

def _hourly(values_by_pollutant, hours=30):
    idx = pd.date_range("2026-11-01", periods=hours, freq="h", tz="Asia/Kolkata")
    return pd.DataFrame(values_by_pollutant, index=idx)


def test_24h_average_uses_only_last_24_hours():
    # First 6 hours are huge, last 24 are 50 -> the old hours must be ignored
    pm = [1000] * 6 + [50] * 24
    out = concentrations_from_hourly(_hourly({"PM2.5": pm}))
    assert out["PM2.5"] == pytest.approx(50)


def test_8h_pollutants_use_max_rolling_mean():
    o3 = [10] * 22 + [100] * 8  # last 8 hours are high
    out = concentrations_from_hourly(_hourly({"O3": o3}))
    assert out["O3"] == pytest.approx(100)


def test_24h_average_requires_16_hours():
    pm = [np.nan] * 15 + [50] * 15  # only 15 valid hours in the last 24
    out = concentrations_from_hourly(_hourly({"PM2.5": pm}))
    assert "PM2.5" not in out
