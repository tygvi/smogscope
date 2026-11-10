"""Fetch CPCB station readings through OpenAQ (https://openaq.org), API v3.

Why a second route to the same stations? data.gov.in refuses connections from outside
India (design-qa Q47), so a server abroad can't use it. OpenAQ collects CPCB's data and
serves it to everyone. Measured on 10 Nov 2026: 576 CPCB locations in India, ~480 of
them reporting within 3 hours, newest reading ~70 minutes old (Q48).

How one fetch works (a few dozen requests, well inside the free plan's 60/minute):
  1. GET /v3/locations?iso=IN          -> every Indian location with its sensors.
                                          Keep provider "CPCB", fixed (not mobile).
  2. For each pollutant parameter those sensors use:
     GET /v3/parameters/{id}/latest     -> the latest value of EVERY sensor worldwide
                                          for that pollutant; keep only our sensors.
  Per-station requests (/locations/{id}/latest) would need ~576 calls per run.

The key travels in a header (X-API-Key), never in the URL, so it can't leak into
error messages the way data.gov.in's did (Q39).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from aqi_india.fetchers.cpcb import USER_AGENT

log = logging.getLogger(__name__)

BASE_URL = "https://api.openaq.org/v3"
PROVIDER = "CPCB"            # skip low-cost sensors (AirGradient, ...) and duplicate feeds
PAGE_SIZE = 1000             # the API's maximum
MAX_PAGES = 60               # safety stop per endpoint
MIN_SECONDS_BETWEEN_CALLS = 1.1   # free plan: 60 requests/minute -> never more than ~55

# OpenAQ parameter names that the CPCB AQI uses (they match our pollutant codes).
POLLUTANTS = {"pm25", "pm10", "no2", "so2", "co", "o3", "nh3"}

# Molar masses (g/mol), to convert ppm/ppb into µg/m³ at 25 °C and 1 atm,
# where one mole of gas fills 24.45 litres.
MOLAR_MASS = {"no2": 46.01, "so2": 64.07, "co": 28.01, "o3": 48.00, "nh3": 17.03}
MOLAR_VOLUME = 24.45


class OpenAQError(RuntimeError):
    """OpenAQ failed or answered with something unusable."""


def to_cpcb_units(pollutant: str, value: float | None, units: str) -> float | None:
    """Convert a reading into the units the CPCB formula expects:
    µg/m³ for everything, except CO in mg/m³. Returns None for units we don't know."""
    if value is None:
        return None
    u = units.strip().lower().replace("μ", "µ").replace("ug", "µg").replace("m3", "m³")
    if u == "µg/m³":
        ug = value
    elif u == "mg/m³":
        ug = value * 1000
    elif u in ("ppm", "ppb") and pollutant in MOLAR_MASS:
        ppb = value * 1000 if u == "ppm" else value
        ug = ppb * MOLAR_MASS[pollutant] / MOLAR_VOLUME
    else:
        return None
    return ug / 1000 if pollutant == "co" else ug


def _session(api_key: str) -> requests.Session:
    """Retries 429 (rate limited) and 5xx with growing waits; honours Retry-After."""
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    session = requests.Session()
    session.headers.update({"X-API-Key": api_key, "User-Agent": USER_AGENT, "Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class _Client:
    def __init__(self, api_key: str):
        if not api_key:
            raise OpenAQError("OPENAQ_API_KEY is not set")
        self.session = _session(api_key)
        self.calls = 0
        self._last = 0.0

    def get(self, path: str, params: dict) -> dict:
        wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)                       # pace ourselves instead of hitting 429s
        self._last = time.monotonic()
        self.calls += 1
        try:
            resp = self.session.get(BASE_URL + path, params=params, timeout=(10, 60))
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise OpenAQError(f"OpenAQ request {path} failed: {e}") from None
        except ValueError:
            raise OpenAQError(f"OpenAQ {path} returned non-JSON (HTTP {resp.status_code})") from None

    def pages(self, path: str, params: dict):
        """Yield every result of a paginated endpoint."""
        for page in range(1, MAX_PAGES + 1):
            results = self.get(path, {**params, "limit": PAGE_SIZE, "page": page}).get("results", [])
            yield from results
            if len(results) < PAGE_SIZE:
                return
        log.warning("OpenAQ %s: stopped after %d pages (MAX_PAGES)", path, MAX_PAGES)


def _cpcb_sensors(client: _Client) -> dict[int, tuple[dict, dict]]:
    """{sensor_id: (location, parameter)} for every pollutant sensor of a fixed CPCB station in India."""
    sensors = {}
    for loc in client.pages("/locations", {"iso": "IN"}):
        if (loc.get("provider") or {}).get("name") != PROVIDER or loc.get("isMobile"):
            continue
        for sensor in loc.get("sensors") or []:
            parameter = sensor.get("parameter") or {}
            if parameter.get("name") in POLLUTANTS:
                sensors[sensor["id"]] = (loc, parameter)
    return sensors


def fetch_records(api_key: str, hours_back: int = 3) -> list[dict]:
    """Latest reading of every CPCB sensor in India from the last `hours_back` hours.

    Returns flat records (one per sensor), every field copied from OpenAQ's answers:
    {sensor_id, location_id, station, locality, latitude, longitude,
     parameter, units, value, datetime_utc}
    """
    client = _Client(api_key)
    sensors = _cpcb_sensors(client)
    if not sensors:
        raise OpenAQError("OpenAQ listed no CPCB sensors in India")
    parameter_ids = sorted({parameter["id"] for _, parameter in sensors.values()})
    log.info("OpenAQ: %d CPCB sensors at %d stations, %d parameters",
             len(sensors), len({loc["id"] for loc, _ in sensors.values()}), len(parameter_ids))

    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = []
    for parameter_id in parameter_ids:
        for row in client.pages(f"/parameters/{parameter_id}/latest", {"datetime_min": since}):
            hit = sensors.get(row.get("sensorsId"))
            if hit is None:
                continue                          # some other country's sensor
            loc, parameter = hit
            coords = loc.get("coordinates") or {}
            records.append({
                "sensor_id": row["sensorsId"],
                "location_id": loc["id"],
                "station": loc.get("name"),
                "locality": loc.get("locality"),
                "latitude": coords.get("latitude"),
                "longitude": coords.get("longitude"),
                "parameter": parameter["name"],
                "units": parameter.get("units"),
                "value": row.get("value"),
                "datetime_utc": (row.get("datetime") or {}).get("utc"),
            })
    if not records:
        # CPCB always has hundreds of fresh readings: none means something is broken (same rule as Q39).
        raise OpenAQError(f"OpenAQ returned no CPCB readings newer than {since}")
    log.info("OpenAQ: %d readings in %d requests", len(records), client.calls)
    return records
