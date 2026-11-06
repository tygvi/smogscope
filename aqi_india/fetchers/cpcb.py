"""Fetch official station data from CPCB via the data.gov.in API.

Dataset: "Real time Air Quality Index from various locations"
https://www.data.gov.in/resource/real-time-air-quality-index-various-locations

Each record is one (station, pollutant) pair with min/max/avg values and a
`last_update` timestamp. The API is paginated with `limit` + `offset`.

The field names have changed over the years (e.g. `pollutant_avg` -> `avg_value`),
so the parser below accepts either. On your first real run, eyeball a few rows
(especially CO) to confirm the units look right before trusting the AQI numbers.
"""
from __future__ import annotations

import logging

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from aqi_india.config import TIMEZONE
from aqi_india.fetchers.open_meteo import AQ_COLUMNS

log = logging.getLogger(__name__)

URL = "https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
SOURCE = "cpcb"

POLLUTANT_MAP = {
    "PM2.5": "PM2.5",
    "PM10": "PM10",
    "NO2": "NO2",
    "SO2": "SO2",
    "CO": "CO",
    "OZONE": "O3",
    "NH3": "NH3",
}


class CPCBResponseError(RuntimeError):
    """data.gov.in answered, but not with usable JSON (an expected, temporary failure)."""


def _session() -> requests.Session:
    """A session that automatically retries when data.gov.in has a bad moment.

    The API often returns 502/503/504 or hangs; retrying with exponential backoff
    (waits of 2 s, 4 s, ...) usually gets through without us writing a manual loop.
    """
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _fetch_all_records(api_key: str, page_size: int, max_pages: int) -> list[dict]:
    records: list[dict] = []
    offset = 0
    session = _session()
    for _ in range(max_pages):
        resp = session.get(
            URL,
            params={"api-key": api_key, "format": "json", "limit": page_size, "offset": offset},
            timeout=(10, 90),  # (connect, read) seconds
        )
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as e:
            raise CPCBResponseError(
                f"data.gov.in returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]!r}"
            ) from e
        batch = payload.get("records", [])
        records.extend(batch)
        offset += len(batch)
        total = int(payload.get("total", 0) or 0)
        if not batch or offset >= total:
            break
    log.info("CPCB: fetched %d records", len(records))
    return records


def fetch_cpcb(api_key: str, page_size: int = 500, max_pages: int = 20) -> pd.DataFrame:
    """Latest pollutant averages for every CPCB station, in the same long format
    as open_meteo.fetch_air_quality()."""
    df = pd.DataFrame(_fetch_all_records(api_key, page_size, max_pages))
    if df.empty:
        return pd.DataFrame(columns=AQ_COLUMNS)

    value_col = "avg_value" if "avg_value" in df.columns else "pollutant_avg"
    df["value"] = pd.to_numeric(df[value_col], errors="coerce")  # "NA" -> NaN
    df["pollutant"] = df["pollutant_id"].str.strip().str.upper().map(
        {k.upper(): v for k, v in POLLUTANT_MAP.items()}
    )
    df["observed_at"] = pd.to_datetime(
        df["last_update"], format="%d-%m-%Y %H:%M:%S", errors="coerce"
    ).dt.tz_localize(TIMEZONE)
    df["lat"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["lon"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["unit"] = df["pollutant"].map(lambda p: "mg/m³" if p == "CO" else "µg/m³")
    df["location"] = df["station"]
    df["source"] = SOURCE

    before = len(df)
    df = df.dropna(subset=["value", "pollutant", "observed_at"])
    log.info("CPCB: kept %d of %d rows after dropping missing values", len(df), before)
    return df[AQ_COLUMNS].reset_index(drop=True)
