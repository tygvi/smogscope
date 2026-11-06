"""Data-quality checks for air-quality readings.

Design (see docs/design-qa.md, Q9-Q11):
- Raw data is never deleted or changed. Each reading gets a `flag` + `flag_reason`.
- These checks run AFTER readings are stored, so they can use history.
- INVALID = physically impossible. SUSPECT = unlikely, probably a sensor problem.

Input everywhere: a long-format DataFrame like the `air_quality` table, with at least
the columns: source, location, observed_at (datetime), pollutant, value.
The DataFrame's index must be unique (results are aligned back to it).

Every check_* function returns a boolean Series with the SAME INDEX as `df`:
True = "this row has the problem".
"""
from __future__ import annotations

import pandas as pd

FLAG_OK = "ok"
FLAG_SUSPECT = "suspect"
FLAG_INVALID = "invalid"

# Columns that identify one sensor's time series. Any check that looks at
# neighbouring readings must group by these first.
SERIES_KEYS = ["source", "location", "pollutant"]

# Upper plausibility limits: above these, a reading is treated as impossible (INVALID).
# Deliberately generous: the goal is to catch sensor garbage (e.g. 99999, or a unit
# mix-up giving 1000x), never to throw away a genuinely terrible pollution episode.
# Units: µg/m³, except CO in mg/m³.
MAX_PLAUSIBLE: dict[str, float] = {
    "PM2.5": 1500,  # Delhi's worst hourly peaks (Diwali night, Nov smog) approach ~1000; margin on top
    "PM10": 3000,   # desert dust storms push hourly PM10 well past 1000 in Rajasthan / NCR
    "NO2": 1000,    # even roadside hourly NO2 rarely exceeds a few hundred
    "SO2": 2000,    # spikes near smelters / coal plants; CPCB's "Severe" band starts at 1600
    "CO": 50,       # mg/m³; "Severe" starts at 34, heavy traffic peaks are typically < 20
    "O3": 1000,     # extreme urban hourly O3 stays well below this
    "NH3": 3000,    # agricultural / industrial peaks; "Severe" starts at 1800
}

# Stuck-sensor minimum level: repeats AT OR BELOW this are believable (clean,
# stable air + values reported rounded), so they are NOT flagged. Set at roughly
# the lower half of each pollutant's "Good" band.
STUCK_MIN_LEVEL: dict[str, float] = {
    "PM2.5": 15,   # Good band: 0-30
    "PM10": 25,    # Good band: 0-50
    "NO2": 15,     # Good band: 0-40
    "SO2": 15,     # Good band: 0-40; clean cities sit at 2-10 for days
    "CO": 0.5,     # mg/m³; Good band: 0-1.0
    "O3": 20,      # Good band: 0-50; drops near zero at night
    "NH3": 50,     # Good band: 0-200
}

STUCK_MIN_RUN = 6  # decided in Q11


def _all_false(df: pd.DataFrame) -> pd.Series:
    return pd.Series(False, index=df.index, dtype=bool)


def check_range(df: pd.DataFrame) -> pd.Series:
    """True where the value is impossible: negative, or above MAX_PLAUSIBLE[pollutant].

    A pollutant missing from MAX_PLAUSIBLE only gets the negative check.
    """
    limits = df["pollutant"].map(MAX_PLAUSIBLE)  # NaN for unknown pollutants
    too_high = df["value"] > limits              # comparison with NaN -> False
    return (df["value"] < 0) | too_high


def check_stuck(
    df: pd.DataFrame,
    min_run: int = STUCK_MIN_RUN,
    min_level: dict[str, float] | None = None,
) -> pd.Series:
    """True for every reading that is part of a "stuck" run:
    >= min_run identical values, at consecutive hours (no gaps),
    within the same source + location + pollutant,
    and with the value above min_level[pollutant] (0 if not listed).
    """
    if df.empty:
        return _all_false(df)
    min_level = STUCK_MIN_LEVEL if min_level is None else min_level

    # 1. Put each sensor's readings next to each other, in time order.
    s = df.sort_values(SERIES_KEYS + ["observed_at"])

    # 2. A row CONTINUES the previous row's run only if all three hold:
    same_series = (s[SERIES_KEYS] == s[SERIES_KEYS].shift()).all(axis=1)
    same_value = s["value"].eq(s["value"].shift())
    one_hour_later = s["observed_at"].diff().eq(pd.Timedelta(hours=1))
    new_run = ~(same_series & same_value & one_hour_later)

    # 3. Every True starts a new run -> running count gives each run a unique id.
    run_id = new_run.cumsum()

    # 4. Run length, broadcast back to every row of the run.
    run_length = run_id.groupby(run_id).transform("size")

    # 5. Long enough AND high enough to be suspicious.
    level = s["pollutant"].map(min_level).fillna(0)
    stuck = (run_length >= min_run) & (s["value"] > level)

    # 6. Back to the caller's original row order.
    return stuck.reindex(df.index)


def check_pm_ratio(df: pd.DataFrame) -> pd.Series:
    """True for the PM2.5 AND PM10 rows where PM2.5 > PM10
    at the same source + location + observed_at.

    PM10 includes PM2.5, so PM2.5 > PM10 is physically impossible: a sensor or
    calibration error. Both rows are flagged, since we can't tell which one is wrong.
    """
    keys = ["source", "location", "observed_at"]
    is_pm = df["pollutant"].isin(["PM2.5", "PM10"])
    pm = df[is_pm]
    if pm.empty:
        return _all_false(df)

    # Long -> wide: one row per (source, location, time), PM2.5 and PM10 side by side.
    wide = pm.pivot_table(index=keys, columns="pollutant", values="value", aggfunc="mean")
    if not {"PM2.5", "PM10"} <= set(wide.columns):
        return _all_false(df)
    bad_keys = wide.index[wide["PM2.5"] > wide["PM10"]]  # NaN comparisons -> False

    # Wide -> back to long: is each row's (source, location, time) one of the bad ones?
    row_keys = pd.MultiIndex.from_frame(df[keys])
    return pd.Series(row_keys.isin(bad_keys), index=df.index) & is_pm


# (check function, flag it sets, reason label) — ordered from least to most severe,
# so a later (more severe) flag overwrites an earlier one.
CHECKS = [
    (check_stuck, FLAG_SUSPECT, "stuck"),
    (check_pm_ratio, FLAG_SUSPECT, "pm25_gt_pm10"),
    (check_range, FLAG_INVALID, "out_of_range"),
]


def assess(df: pd.DataFrame) -> pd.DataFrame:
    """Run all checks and return a copy of df with two new columns:

    flag:        "ok" | "suspect" | "invalid"
    flag_reason: e.g. "out_of_range", "stuck,pm25_gt_pm10", or "" when ok.

    Severity: invalid beats suspect beats ok.
    """
    if not df.index.is_unique:
        raise ValueError("assess() needs a unique index; call df.reset_index(drop=True) first")

    out = df.copy()
    flag = pd.Series(FLAG_OK, index=df.index)
    reason = pd.Series("", index=df.index)

    for check, flag_value, label in CHECKS:
        hit = check(df)
        flag = flag.mask(hit, flag_value)
        reason = reason.mask(hit, reason + "," + label)

    out["flag"] = flag
    out["flag_reason"] = reason.str.lstrip(",")
    return out
