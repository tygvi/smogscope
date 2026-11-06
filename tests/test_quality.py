"""Tests for aqi_india/quality.py.

Run:  pytest tests/test_quality.py -v
"""
import pandas as pd
import pytest

from aqi_india.quality import (
    FLAG_INVALID,
    FLAG_OK,
    FLAG_SUSPECT,
    assess,
    check_pm_ratio,
    check_range,
    check_stuck,
)


def make_readings(values, pollutant="PM2.5", location="Delhi", start="2026-11-01 00:00", hours=None):
    """Build a tiny long-format DataFrame of hourly readings.

    values: list of numbers, one per hour
    hours:  optional list of hour offsets, to create GAPS (e.g. [0, 1, 2, 5, 6])
    """
    hours = hours if hours is not None else list(range(len(values)))
    times = pd.Timestamp(start, tz="Asia/Kolkata") + pd.to_timedelta(hours, unit="h")
    return pd.DataFrame({
        "source": "cpcb",
        "location": location,
        "observed_at": times,
        "pollutant": pollutant,
        "value": values,
    })


def combine(*frames):
    return pd.concat(frames, ignore_index=True)


# ---- check_range ----

def test_range_flags_negative_values():
    assert check_range(make_readings([50, -3, 60])).tolist() == [False, True, False]


def test_range_flags_values_above_limit():
    assert check_range(make_readings([120, 5000])).tolist() == [False, True]


def test_range_uses_per_pollutant_limits():
    # 40 mg/m³ CO is fine; 60 is above the CO limit. 60 µg/m³ PM2.5 is normal.
    df = combine(make_readings([40, 60], pollutant="CO"), make_readings([60], pollutant="PM2.5"))
    assert check_range(df).tolist() == [False, True, False]


def test_range_unknown_pollutant_only_checks_negative():
    assert check_range(make_readings([1e9, -1], pollutant="benzene")).tolist() == [False, True]


# ---- check_stuck ----

def test_stuck_six_identical_values_flagged():
    assert check_stuck(make_readings([118] * 6)).all()


def test_stuck_five_identical_values_not_flagged():
    assert not check_stuck(make_readings([118] * 5)).any()


def test_stuck_only_the_run_is_flagged():
    flags = check_stuck(make_readings([90, 118, 118, 118, 118, 118, 118, 95]))
    assert flags.tolist() == [False] + [True] * 6 + [False]


def test_stuck_gap_breaks_the_run():
    # 6 identical values, but hour 3 is missing -> two runs of 3
    df = make_readings([118] * 6, hours=[0, 1, 2, 4, 5, 6])
    assert not check_stuck(df).any()


def test_stuck_low_values_are_believable():
    assert not check_stuck(make_readings([4] * 8, pollutant="SO2")).any()


def test_stuck_runs_do_not_cross_locations():
    # Delhi's last 3 hours and Mumbai's first 3 hours are not one run
    df = combine(make_readings([118] * 3, location="Delhi"),
                 make_readings([118] * 3, location="Mumbai", start="2026-11-01 03:00"))
    assert not check_stuck(df).any()


def test_stuck_result_aligns_with_shuffled_input():
    df = make_readings([90, 118, 118, 118, 118, 118, 118, 95])
    shuffled = df.sample(frac=1, random_state=0)
    flags = check_stuck(shuffled)
    assert flags.index.equals(shuffled.index)
    assert flags.sort_index().tolist() == [False] + [True] * 6 + [False]


def test_stuck_empty_input():
    assert check_stuck(make_readings([])).empty


# ---- check_pm_ratio ----

def test_pm_ratio_flags_both_rows():
    df = combine(make_readings([80], pollutant="PM2.5"), make_readings([60], pollutant="PM10"))
    assert check_pm_ratio(df).tolist() == [True, True]


def test_pm_ratio_normal_not_flagged():
    df = combine(make_readings([40], pollutant="PM2.5"), make_readings([60], pollutant="PM10"))
    assert not check_pm_ratio(df).any()


def test_pm_ratio_different_times_not_compared():
    df = combine(make_readings([80], pollutant="PM2.5", start="2026-11-01 10:00"),
                 make_readings([60], pollutant="PM10", start="2026-11-01 11:00"))
    assert not check_pm_ratio(df).any()


def test_pm_ratio_ignores_other_pollutants():
    df = combine(make_readings([80], pollutant="PM2.5"),
                 make_readings([60], pollutant="PM10"),
                 make_readings([30], pollutant="NO2"))
    assert check_pm_ratio(df).tolist() == [True, True, False]


def test_pm_ratio_without_pm10():
    assert not check_pm_ratio(make_readings([80, 90])).any()


# ---- assess ----

def test_assess_ok_rows():
    out = assess(make_readings([50, 60, 70]))
    assert (out["flag"] == FLAG_OK).all()
    assert (out["flag_reason"] == "").all()


def test_assess_invalid_beats_suspect():
    # PM2.5 = 5000 is out of range AND greater than PM10 -> invalid, both reasons listed
    df = combine(make_readings([5000], pollutant="PM2.5"), make_readings([60], pollutant="PM10"))
    out = assess(df)
    assert out.loc[0, "flag"] == FLAG_INVALID
    assert out.loc[0, "flag_reason"] == "pm25_gt_pm10,out_of_range"
    assert out.loc[1, "flag"] == FLAG_SUSPECT
    assert out.loc[1, "flag_reason"] == "pm25_gt_pm10"


def test_assess_does_not_modify_input():
    df = make_readings([118] * 6)
    assess(df)
    assert "flag" not in df.columns


def test_assess_rejects_duplicate_index():
    df = pd.concat([make_readings([1]), make_readings([2])])  # both rows have index 0
    with pytest.raises(ValueError):
        assess(df)
