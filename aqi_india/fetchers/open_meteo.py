"""Fetch weather + air quality from Open-Meteo (free, no API key).

Note: Open-Meteo's air-quality numbers are *model estimates* (Copernicus CAMS),
not ground-station measurements. They cover every city evenly, which makes them
a good baseline; CPCB (cpcb.py) gives the official station measurements.

Both endpoints accept many comma-separated coordinates in a single request and
return a list of results in the same order, so one call covers all cities.
"""
from __future__ import annotations

import pandas as pd
import requests

from aqi_india.config import TIMEZONE, City

AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE = "open-meteo"

# Open-Meteo variable -> (our pollutant name, multiplier to our unit, our unit)
AQ_VARIABLES: dict[str, tuple[str, float, str]] = {
    "pm10": ("PM10", 1.0, "µg/m³"),
    "pm2_5": ("PM2.5", 1.0, "µg/m³"),
    "nitrogen_dioxide": ("NO2", 1.0, "µg/m³"),
    "sulphur_dioxide": ("SO2", 1.0, "µg/m³"),
    "ozone": ("O3", 1.0, "µg/m³"),
    "carbon_monoxide": ("CO", 0.001, "mg/m³"),  # Open-Meteo gives µg/m³; CPCB uses mg/m³
}

# Open-Meteo variable -> our column name
WEATHER_VARIABLES: dict[str, str] = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "humidity_pct",
    "wind_speed_10m": "wind_speed_kmh",
    "wind_direction_10m": "wind_dir_deg",
    "surface_pressure": "pressure_hpa",
    "precipitation": "precipitation_mm",
    "cloud_cover": "cloud_cover_pct",
}

AQ_COLUMNS = ["source", "location", "state", "lat", "lon", "observed_at", "pollutant", "value", "unit"]


def _get(url: str, cities: list[City], extra_params: dict) -> list[dict]:
    params = {
        "latitude": ",".join(str(c.lat) for c in cities),
        "longitude": ",".join(str(c.lon) for c in cities),
        "timezone": TIMEZONE,
        **extra_params,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else [data]  # single location -> plain dict


def fetch_air_quality(cities: list[City], past_days: int = 2) -> pd.DataFrame:
    """Hourly pollutant concentrations for the last `past_days`, in long format
    (one row per city x hour x pollutant)."""
    results = _get(AIR_QUALITY_URL, cities, {
        "hourly": ",".join(AQ_VARIABLES),
        "past_days": past_days,
        "forecast_days": 1,
    })
    now = pd.Timestamp.now(tz=TIMEZONE)

    frames = []
    for city, result in zip(cities, results, strict=True):
        hourly = pd.DataFrame(result["hourly"])
        hourly["observed_at"] = pd.to_datetime(hourly.pop("time")).dt.tz_localize(TIMEZONE)
        hourly = hourly[hourly["observed_at"] <= now]  # drop forecast hours

        long = hourly.melt(id_vars="observed_at", var_name="variable", value_name="value")
        long = long.dropna(subset=["value"])
        mapping = long["variable"].map(AQ_VARIABLES)
        long["pollutant"] = mapping.str[0]
        long["value"] = long["value"] * mapping.str[1]
        long["unit"] = mapping.str[2]
        long = long.assign(source=SOURCE, location=city.name, state=city.state, lat=city.lat, lon=city.lon)
        frames.append(long[AQ_COLUMNS])

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=AQ_COLUMNS)


def fetch_wind_grid(
    lat_min: float = 6.0, lat_max: float = 36.0,
    lon_min: float = 68.0, lon_max: float = 98.0,
    step: float = 2.0,
) -> dict:
    """Current 10 m wind on a regular lat/lon grid covering India, as u/v components.

    Returns a JSON-friendly dict:
      lat0, lon0, dlat, dlon, nx (columns, west->east), ny (rows, south->north),
      u, v: flat lists of length nx*ny in row-major order (index = row * nx + col), m/s,
      observed_at: ISO time of the model step.

    u = eastward component, v = northward component. Meteorological direction is
    where the wind comes FROM, so a wind "from 0°" (north) blows south -> v < 0.

    Open-Meteo counts every location toward its free daily limit, so the grid is
    deliberately coarse (2° -> 16 x 16 = 256 points) and callers should cache it.
    """
    import math

    lats = [lat_min + i * step for i in range(int(round((lat_max - lat_min) / step)) + 1)]
    lons = [lon_min + j * step for j in range(int(round((lon_max - lon_min) / step)) + 1)]
    points = [City("", "", lat, lon) for lat in lats for lon in lons]  # row-major, south->north

    results = _get(WEATHER_URL, points, {
        "current": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms",
    })

    u, v = [], []
    for result in results:
        speed = result["current"]["wind_speed_10m"] or 0.0
        direction = math.radians(result["current"]["wind_direction_10m"] or 0.0)
        u.append(round(-speed * math.sin(direction), 2))
        v.append(round(-speed * math.cos(direction), 2))

    return {
        "lat0": lats[0], "lon0": lons[0], "dlat": step, "dlon": step,
        "nx": len(lons), "ny": len(lats), "u": u, "v": v, "unit": "m/s",
        "observed_at": (pd.Timestamp(results[0]["current"]["time"]).tz_localize(TIMEZONE).isoformat()
                        if results else None),
    }


def fetch_weather(cities: list[City]) -> pd.DataFrame:
    """Current weather conditions, one row per city."""
    results = _get(WEATHER_URL, cities, {"current": ",".join(WEATHER_VARIABLES)})

    rows = []
    for city, result in zip(cities, results, strict=True):
        current = result["current"]
        row = {
            "source": SOURCE,
            "location": city.name,
            "state": city.state,
            "lat": city.lat,
            "lon": city.lon,
            "observed_at": pd.Timestamp(current["time"]).tz_localize(TIMEZONE),
        }
        row.update({ours: current.get(theirs) for theirs, ours in WEATHER_VARIABLES.items()})
        rows.append(row)
    return pd.DataFrame(rows)
