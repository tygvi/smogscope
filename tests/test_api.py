"""Tests for the FastAPI app, against a small throwaway database."""
import json

import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient

from aqi_india import api, db

TZ = "Asia/Kolkata"


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    conn = db.connect(path)
    now = pd.Timestamp.now(tz=TZ).floor("h")
    hours = [now - pd.Timedelta(hours=h) for h in (2, 1, 0)]

    def aqi_rows(location, lat, lon, values, category, dominant):
        return pd.DataFrame({
            "source": "open-meteo", "location": location, "state": "S", "lat": lat, "lon": lon,
            "observed_at": hours, "aqi": values, "category": category,
            "dominant_pollutant": dominant, "sub_indices": json.dumps({dominant: values[-1]}),
        })

    db.upsert(conn, "aqi", pd.concat([
        aqi_rows("Delhi", 28.6, 77.2, [300, 310, 320], "Very Poor", "PM2.5"),
        aqi_rows("Chennai", 13.1, 80.3, [60, 55, 50], "Good", "O3"),
    ], ignore_index=True))
    db.upsert(conn, "air_quality", pd.DataFrame({
        "source": "open-meteo", "location": "Delhi", "state": "S", "lat": 28.6, "lon": 77.2,
        "observed_at": hours, "pollutant": "PM2.5", "value": [150.0, 9999.0, 160.0], "unit": "µg/m³",
    }))
    conn.execute("UPDATE air_quality SET flag = 'invalid' WHERE value = 9999.0")
    db.upsert(conn, "weather", pd.DataFrame({
        "source": "open-meteo", "location": ["Delhi"], "state": "S", "lat": 28.6, "lon": 77.2,
        "observed_at": [hours[-1]], "temperature_c": [24.5], "humidity_pct": [60.0],
        "wind_speed_kmh": [5.0], "wind_dir_deg": [300.0], "pressure_hpa": [1010.0],
        "precipitation_mm": [0.0], "cloud_cover_pct": [10.0],
    }))
    db.upsert(conn, "daily_summary", pd.DataFrame({
        "source": "open-meteo", "location": "Delhi", "date": ["2026-11-05", "2026-11-06"],
        "state": "S", "lat": 28.6, "lon": 77.2, "aqi_mean": [320.0, 310.0], "aqi_min": [300, 290],
        "aqi_max": [340, 330], "aqi_hours": [24, 20], "dominant_pollutant": "PM2.5",
        "worst_category": "Very Poor", "readings_total": [144, 120], "readings_ok": [144, 118],
        "pct_ok": [100.0, 98.3],
    }))
    conn.commit()
    conn.close()

    def get_test_conn():
        c = db.connect(path, check_same_thread=False)
        try:
            yield c
        finally:
            c.close()

    api.app.dependency_overrides[api.get_conn] = get_test_conn
    api._wind_cache.clear()
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    api._wind_cache.clear()


def test_cities_latest_worst_first_with_weather(client):
    body = client.get("/api/cities").json()
    assert [c["location"] for c in body] == ["Delhi", "Chennai"]
    assert body[0]["aqi"] == 320                      # latest hour, not the first
    assert body[0]["sub_indices"] == {"PM2.5": 320}   # JSON text -> real object
    assert body[0]["weather"]["temperature_c"] == 24.5
    assert body[1]["weather"] is None                 # no weather row for Chennai


def test_overview(client):
    body = client.get("/api/overview").json()
    assert body["cities"] == 2
    assert body["average_aqi"] == 185.0
    assert body["counts_by_category"] == {"Very Poor": 1, "Good": 1}
    assert (body["worst"]["location"], body["best"]["location"]) == ("Delhi", "Chennai")


def test_history_excludes_flagged_readings(client):
    body = client.get("/api/cities/Delhi/history?hours=6").json()
    assert [p["aqi"] for p in body["aqi"]] == [300, 310, 320]
    assert [p["value"] for p in body["pollutants"]["PM2.5"]] == [150.0, 160.0]  # 9999 was invalid
    assert body["units"] == {"PM2.5": "µg/m³"}


def test_history_unknown_city_is_404(client):
    assert client.get("/api/cities/Atlantis/history").status_code == 404


def test_history_validates_hours(client):
    assert client.get("/api/cities/Delhi/history?hours=0").status_code == 422
    assert client.get("/api/cities/Delhi/history?hours=abc").status_code == 422


def test_daily_oldest_first(client):
    body = client.get("/api/cities/Delhi/daily?days=7").json()
    assert [d["date"] for d in body] == ["2026-11-05", "2026-11-06"]
    assert body[1]["pct_ok"] == 98.3


def test_timeline_shared_time_axis(client):
    body = client.get("/api/timeline?hours=6").json()
    assert len(body["times"]) == 3
    by_city = {c["location"]: c["aqi"] for c in body["cities"]}
    assert by_city == {"Delhi": [300, 310, 320], "Chennai": [60, 55, 50]}


def _grid():
    return {"lat0": 6.0, "lon0": 68.0, "dlat": 2.0, "dlon": 2.0, "nx": 2, "ny": 2,
            "u": [1.0, 2.0, 3.0, 4.0], "v": [0.0, 0.0, 0.0, 0.0], "unit": "m/s", "observed_at": None}


def test_wind_is_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_fetch_wind", lambda: calls.append(1) or _grid())
    assert client.get("/api/wind").json()["u"] == [1.0, 2.0, 3.0, 4.0]
    client.get("/api/wind")
    assert len(calls) == 1   # second request served from cache


def test_wind_serves_stale_cache_when_refresh_fails(client, monkeypatch):
    monkeypatch.setattr(api, "_fetch_wind", _grid)
    client.get("/api/wind")
    api._wind_cache["cached_at"] = 0   # pretend the cache expired

    def broken():
        raise requests.ConnectionError("down")
    monkeypatch.setattr(api, "_fetch_wind", broken)
    assert client.get("/api/wind").status_code == 200


def test_wind_503_without_any_cache(client, monkeypatch):
    def broken():
        raise requests.ConnectionError("down")
    monkeypatch.setattr(api, "_fetch_wind", broken)
    assert client.get("/api/wind").status_code == 503
