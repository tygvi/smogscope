"""Indian National Air Quality Index (NAQI), as defined by CPCB (2014).

How the Indian AQI works
------------------------
1. Each pollutant's concentration is averaged over a standard window:
   - PM10, PM2.5, NO2, SO2, NH3 -> 24-hour average
   - CO, O3                     -> 8-hour average (we use the max 8-h mean in the last 24 h)
2. Each average is converted to a *sub-index* (0-500) by linear interpolation
   between breakpoints:   I = I_lo + (I_hi - I_lo) / (B_hi - B_lo) * (C - B_lo)
3. The AQI is the MAXIMUM sub-index; that pollutant is the "dominant pollutant".
4. Validity rule: at least 3 pollutants must be available, and one of them
   must be PM2.5 or PM10.

Units: µg/m³ for everything except CO, which is mg/m³.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

# Sub-index bands shared by every pollutant.
INDEX_BANDS: list[tuple[int, int]] = [
    (0, 50), (51, 100), (101, 200), (201, 300), (301, 400), (401, 500),
]

# Concentration breakpoints (B_lo, B_hi) per pollutant, one per band above.
# CPCB publishes the "Severe" band as open-ended (e.g. PM2.5 "250+"); the upper
# bounds used here for that last band follow common practice and the result is
# capped at 500 anyway.
CONC_BREAKPOINTS: dict[str, list[tuple[float, float]]] = {
    "PM10":  [(0, 50), (50, 100), (100, 250), (250, 350), (350, 430), (430, 510)],
    "PM2.5": [(0, 30), (30, 60), (60, 90), (90, 120), (120, 250), (250, 380)],
    "NO2":   [(0, 40), (40, 80), (80, 180), (180, 280), (280, 400), (400, 520)],
    "SO2":   [(0, 40), (40, 80), (80, 380), (380, 800), (800, 1600), (1600, 2400)],
    "CO":    [(0, 1.0), (1.0, 2.0), (2.0, 10), (10, 17), (17, 34), (34, 51)],  # mg/m³
    "O3":    [(0, 50), (50, 100), (100, 168), (168, 208), (208, 748), (748, 1028)],
    "NH3":   [(0, 200), (200, 400), (400, 800), (800, 1200), (1200, 1800), (1800, 3000)],
}

EIGHT_HOUR_POLLUTANTS = {"CO", "O3"}
MIN_HOURS_FOR_24H_AVG = 16  # CPCB requires at least 16 of 24 hourly values

CATEGORIES: list[tuple[int, str]] = [
    (50, "Good"),
    (100, "Satisfactory"),
    (200, "Moderate"),
    (300, "Poor"),
    (400, "Very Poor"),
    (500, "Severe"),
]


@dataclass(frozen=True)
class AQIResult:
    aqi: int
    category: str
    dominant_pollutant: str
    sub_indices: dict[str, int]


def _round_half_up(x: float) -> int:
    # Python's round() uses banker's rounding (round(50.5) == 50); AQI should round 50.5 -> 51.
    return math.floor(x + 0.5)


def sub_index(pollutant: str, concentration: float | None) -> float | None:
    """Convert one pollutant's averaged concentration into its 0-500 sub-index."""
    if pollutant not in CONC_BREAKPOINTS:
        raise ValueError(f"Unknown pollutant: {pollutant!r}")
    if concentration is None or pd.isna(concentration) or concentration < 0:
        return None

    for (b_lo, b_hi), (i_lo, i_hi) in zip(CONC_BREAKPOINTS[pollutant], INDEX_BANDS):
        if concentration <= b_hi:
            return i_lo + (i_hi - i_lo) * (concentration - b_lo) / (b_hi - b_lo)
    return 500.0  # beyond the top breakpoint -> cap


def category(aqi: float) -> str:
    for upper, name in CATEGORIES:
        if aqi <= upper:
            return name
    return "Severe"


def compute_aqi(concentrations: dict[str, float]) -> AQIResult | None:
    """Compute the AQI from averaged concentrations, e.g. {"PM2.5": 85.0, "NO2": 40.2, ...}.

    Returns None when the CPCB validity rule isn't met.
    """
    subs: dict[str, float] = {}
    for pollutant, conc in concentrations.items():
        if pollutant not in CONC_BREAKPOINTS:
            continue  # silently ignore things we can't index (e.g. dust)
        si = sub_index(pollutant, conc)
        if si is not None:
            subs[pollutant] = si

    has_particulate = bool({"PM2.5", "PM10"} & subs.keys())
    if len(subs) < 3 or not has_particulate:
        return None

    dominant = max(subs, key=subs.get)
    aqi = _round_half_up(subs[dominant])
    return AQIResult(
        aqi=aqi,
        category=category(aqi),
        dominant_pollutant=dominant,
        sub_indices={p: _round_half_up(v) for p, v in subs.items()},
    )


def concentrations_from_hourly(hourly: pd.DataFrame) -> dict[str, float]:
    """Turn hourly readings into the averaged concentrations the AQI formula needs.

    `hourly`: DatetimeIndex (sorted), one column per pollutant, values in the units above.
    Uses the 24 hours ending at the latest timestamp.
    """
    if hourly.empty:
        return {}
    hourly = hourly.sort_index()
    end = hourly.index.max()
    window = hourly[hourly.index > end - pd.Timedelta(hours=24)]

    out: dict[str, float] = {}
    for pollutant in window.columns:
        series = window[pollutant]
        if pollutant in EIGHT_HOUR_POLLUTANTS:
            # Time-based rolling window handles missing hours correctly.
            value = series.rolling("8h", min_periods=6).mean().max()
        else:
            if series.count() < MIN_HOURS_FOR_24H_AVG:
                continue
            value = series.mean()
        if pd.notna(value):
            out[pollutant] = float(value)
    return out
