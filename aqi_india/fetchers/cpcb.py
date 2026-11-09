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
    """data.gov.in failed or answered without usable JSON. The message never contains the API key."""


# data.gov.in's firewall silently stalls requests whose User-Agent is the default
# "python-requests/x.y" and answers with a 502 after ~60 s, which looks exactly like an
# outage. curl's UA and an honest custom one get through in under a second (Q39).
USER_AGENT = "SmogScope/0.1 (+https://github.com/tygvi/smogscope)"


def _redact(text: str, secret: str) -> str:
    """Hide the API key: it is part of the request URL, so it leaks into error messages."""
    return text.replace(secret, "***") if secret else text


def _session() -> requests.Session:
    """A session that identifies itself honestly and retries when data.gov.in has a bad moment.

    Retrying with exponential backoff (waits of 2 s, 4 s, ...) handles real 502/503/504
    blips without us writing a manual loop.
    """
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _fetch_all_records(api_key: str, page_size: int, max_pages: int) -> list[dict]:
    records: list[dict] = []
    offset = 0
    session = _session()
    for page in range(1, max_pages + 1):
        try:
            resp = session.get(
                URL,
                params={"api-key": api_key, "format": "json", "limit": page_size, "offset": offset},
                timeout=(10, 90),  # (connect, read) seconds
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            # `from None` drops the original exception, whose message includes the full URL + key.
            raise CPCBResponseError(f"CPCB request failed on page {page}: {_redact(str(e), api_key)}") from None
        try:
            payload = resp.json()
        except ValueError:
            raise CPCBResponseError(
                f"data.gov.in returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]!r}"
            ) from None
        batch = payload.get("records", [])
        total = int(payload.get("total", 0) or 0)
        if page == 1 and total == 0:
            # A third failure mode: HTTP 200, status "ok", but empty, with a message like
            # "No query was recieved." CPCB always has thousands of records, so an empty
            # first page is a failure, not "no data" (Q39).
            raise CPCBResponseError(f"data.gov.in returned no records: {payload.get('message')!r}")
        records.extend(batch)
        offset += len(batch)
        log.info("CPCB: page %d, %d/%d records", page, offset, total)
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
