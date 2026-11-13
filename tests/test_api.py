"""Tests for the FastAPI app, against a throwaway PostgreSQL database.

The schema comes from the real migrations (sql/001-003), so the tests also prove
the migrations work on an empty database. Skipped if PostgreSQL isn't running.
"""
import psycopg
import pytest
import requests
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from aqi_india import api
from aqi_india.config import PROJECT_ROOT, TIMEZONE

TEST_DB = "smogscope_test"
MIGRATIONS = sorted((PROJECT_ROOT / "sql").glob("[0-9][0-9][0-9]_*.sql"))


@pytest.fixture(scope="session")
def test_dsn():
    try:
        admin = psycopg.connect("dbname=postgres", autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("PostgreSQL is not running")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    dsn = f"dbname={TEST_DB}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in MIGRATIONS:
            conn.execute(path.read_text())
    yield dsn
    with psycopg.connect("dbname=postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture
def client(test_dsn):
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE gold.aqi_hourly, gold.daily_summary, gold.daily_pollutant,"
                     " silver.air_quality, silver.weather, silver.location, bronze.ingest_run CASCADE")
        conn.execute(f"SET TIME ZONE '{TIMEZONE}'")
        _seed(conn)

    def get_test_conn():
        c = psycopg.connect(test_dsn, autocommit=True, row_factory=dict_row)
        c.execute(f"SET TIME ZONE '{TIMEZONE}'")
        try:
            yield c
        finally:
            c.close()

    api.app.dependency_overrides[api.get_conn] = get_test_conn
    api._wind_cache.clear()
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    api._wind_cache.clear()


def _seed(conn):
    """Delhi: 3 model hours + 1 nearby station now (-> measured).
    Chennai: 3 model hours, no station nearby (-> estimated).
    One more station far from every city."""
    def loc(kind, name, city, lat, lon):
        return conn.execute(
            "INSERT INTO silver.location (kind, name, city, state, lat, lon) VALUES (%s, %s, %s, 'S', %s, %s)"
            " RETURNING location_id", (kind, name, city, lat, lon)).fetchone()[0]

    delhi = loc("city", "Delhi", "Delhi", 28.6139, 77.2090)
    chennai = loc("city", "Chennai", "Chennai", 13.0827, 80.2707)
    near = loc("station", "Station A, Delhi", "Delhi", 28.62, 77.21)      # ~1 km from Delhi
    far = loc("station", "Far Station", "Nowhere", 20.0, 75.0)           # no city within 25 km

    hour = "date_trunc('hour', now()) - make_interval(hours => %s)"
    for loc_id, values, cats in [
        (delhi, [300, 310, 320], ["Very Poor", "Very Poor", "Very Poor"]),
        (chennai, [60, 55, 50], ["Satisfactory", "Satisfactory", "Good"]),
    ]:
        for h, (v, cat) in zip((2, 1, 0), zip(values, cats)):
            conn.execute(f"INSERT INTO gold.aqi_hourly (source, location_id, observed_at, aqi, category, dominant, pm25_si)"
                         f" VALUES ('open-meteo', %s, {hour}, %s, %s, 'pm25', %s)", (loc_id, h, v, cat, v))
    conn.execute(f"INSERT INTO gold.aqi_hourly (source, location_id, observed_at, aqi, category, dominant,"
                 f" pm25_si, pm10_si, no2_si) VALUES ('cpcb', %s, {hour}, 350, 'Very Poor', 'pm25', 350, 200, 40)",
                 (near, 0))
    conn.execute(f"INSERT INTO gold.aqi_hourly (source, location_id, observed_at, aqi, category, dominant,"
                 f" pm25_si, pm10_si, no2_si) VALUES ('cpcb', %s, {hour}, 100, 'Satisfactory', 'pm10', 80, 100, 20)",
                 (far, 0))
    for station, subs in [(near, {"pm25": 350, "pm10": 200, "no2": 40}), (far, {"pm25": 80, "pm10": 100, "no2": 20})]:
        for pollutant, v in subs.items():
            conn.execute(f"INSERT INTO silver.air_quality (source, location_id, pollutant, measure, observed_at, value)"
                         f" VALUES ('cpcb', %s, %s, 'sub_index', {hour}, %s)", (station, pollutant, 0, v))

    for h, v, flag in [(2, 150.0, "ok"), (1, 9999.0, "invalid"), (0, 160.0, "ok")]:
        conn.execute(f"INSERT INTO silver.air_quality (source, location_id, pollutant, measure, observed_at, value, flag)"
                     f" VALUES ('open-meteo', %s, 'pm25', 'concentration', {hour}, %s, %s::silver.quality_flag)",
                     (delhi, h, v, flag))
    conn.execute(f"INSERT INTO silver.weather (location_id, observed_at, temperature_c, humidity_pct,"
                 f" wind_speed_kmh, wind_dir_deg) VALUES (%s, {hour}, 24.5, 60, 5, 300)", (delhi, 0))
    conn.execute("INSERT INTO gold.daily_summary (source, location_id, day, mean_aqi, min_aqi, max_aqi,"
                 " worst_category, hours_covered, pct_ok) VALUES"
                 " ('open-meteo', %(d)s, '2026-11-05', 320, 300, 340, 'Very Poor', 24, 100),"
                 " ('open-meteo', %(d)s, '2026-11-06', 310, 290, 330, 'Very Poor', 20, 98.3)", {"d": delhi})


def test_cities_measured_beats_model(client):
    body = client.get("/api/cities").json()
    assert [c["location"] for c in body] == ["Delhi", "Chennai"]
    delhi, chennai = body
    assert delhi["basis"] == "measured" and delhi["aqi"] == 350 and delhi["stations"] == 1
    assert (delhi["measured_aqi"], delhi["model_aqi"], delhi["model_gap"]) == (350, 320, -30)
    assert delhi["sub_indices"] == {"PM2.5": 350, "PM10": 200, "NO2": 40}     # station average
    assert delhi["dominant_pollutant"] == "PM2.5"
    assert delhi["weather"]["temperature_c"] == 24.5
    assert chennai["basis"] == "estimated" and chennai["aqi"] == 50 and chennai["stations"] is None
    assert chennai["sub_indices"] == {"PM2.5": 50}                            # model
    assert chennai["weather"] is None


def test_overview(client):
    body = client.get("/api/overview").json()
    assert (body["cities"], body["measured"]) == (2, 1)
    assert body["average_aqi"] == 200.0
    assert body["counts_by_category"] == {"Very Poor": 1, "Good": 1}
    assert (body["worst"]["location"], body["best"]["location"]) == ("Delhi", "Chennai")


def test_api_headers_are_not_cached(client):
    r = client.get("/api/cities")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" in r.headers


def test_health_reports_stale_and_failed_sources(client, test_dsn):
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO bronze.ingest_run (source, status, started_at) VALUES"
            " ('cpcb', 'ok', now() - interval '4 hours'),"
            " ('open-meteo-aq', 'ok', now()),"
            " ('open-meteo-weather', 'failed', now())")
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["sources"]["cpcb"]["status"] == "stale"
    assert body["sources"]["open-meteo-aq"]["status"] == "ok"
    assert body["sources"]["open-meteo-weather"]["status"] == "failed"
    assert body["sources"]["cpcb"]["age_seconds"] >= 4 * 3600


def test_health_ok_when_all_sources_fresh(client, test_dsn):
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO bronze.ingest_run (source, status, started_at) VALUES"
            " ('cpcb', 'ok', now()), ('open-meteo-aq', 'ok', now()),"
            " ('open-meteo-weather', 'ok', now())")
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert all(s["status"] == "ok" for s in body["sources"].values())


def test_city_measured_hourly_does_not_double_count_same_station(test_dsn):
    """The same station can appear via both feeds (during a feed switch). It is one
    physical station: the freshest reading wins, and `stations` counts it once."""
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE gold.aqi_hourly, silver.air_quality, silver.weather,"
                     " silver.location, bronze.ingest_run CASCADE")
        city = conn.execute(
            "INSERT INTO silver.location (kind, name, city, state, lat, lon)"
            " VALUES ('city', 'Delhi', 'Delhi', 'Delhi', 28.6139, 77.209) RETURNING location_id").fetchone()[0]
        station = conn.execute(
            "INSERT INTO silver.location (kind, name, city, state, lat, lon)"
            " VALUES ('station', 'Station A', 'Delhi', 'Delhi', 28.62, 77.21) RETURNING location_id").fetchone()[0]
        hour = "date_trunc('hour', now())"
        conn.execute(
            f"INSERT INTO gold.aqi_hourly (source, location_id, observed_at, aqi, category, dominant, computed_at)"
            f" VALUES ('cpcb', %s, {hour}, 300, 'Poor', 'pm25', now() - interval '1 hour')", (station,))
        conn.execute(
            f"INSERT INTO gold.aqi_hourly (source, location_id, observed_at, aqi, category, dominant, computed_at)"
            f" VALUES ('openaq', %s, {hour}, 310, 'Poor', 'pm25', now())", (station,))
        aqi, stations = conn.execute(
            "SELECT aqi, stations FROM gold.city_measured_hourly WHERE location_id = %s", (city,)).fetchone()
        assert (aqi, stations) == (310, 1)


def test_stations_prefers_fresher_gold_over_stale_subindices(client, test_dsn):
    """A stale data.gov.in sub-index must not hide a newer OpenAQ gold reading."""
    with psycopg.connect(test_dsn, autocommit=True) as conn:
        station = conn.execute(
            "INSERT INTO silver.location (kind, name, city, state, lat, lon)"
            " VALUES ('station', 'Mixed Feed Station', 'Nowhere', 'S', 15.0, 70.0) RETURNING location_id").fetchone()[0]
        conn.execute(
            "INSERT INTO silver.air_quality (source, location_id, pollutant, measure, observed_at, value)"
            " VALUES ('cpcb', %s, 'pm25', 'sub_index', date_trunc('hour', now()) - interval '2 hours', 111)",
            (station,))
        conn.execute(
            "INSERT INTO gold.aqi_hourly (source, location_id, observed_at, aqi, category, dominant,"
            " pm25_si, pm10_si, no2_si) VALUES ('openaq', %s, date_trunc('hour', now()), 222, 'Poor', 'pm25',"
            " 222, 90, 30)", (station,))

    body = {s["name"]: s for s in client.get("/api/stations").json()}
    s = body["Mixed Feed Station"]
    assert s["aqi"] == 222
    assert s["sub_indices"] == {"PM2.5": 222, "PM10": 90, "NO2": 30}


def test_stations_lists_every_reporting_station(client):
    body = {s["name"]: s for s in client.get("/api/stations").json()}
    assert set(body) == {"Station A, Delhi", "Far Station"}
    a = body["Station A, Delhi"]
    assert (a["aqi"], a["category"], a["dominant_pollutant"]) == (350, "Very Poor", "PM2.5")
    assert a["sub_indices"] == {"PM2.5": 350, "PM10": 200, "NO2": 40}


def test_history_has_model_measured_and_clean_readings(client):
    body = client.get("/api/cities/Delhi/history?hours=6").json()
    assert [p["aqi"] for p in body["aqi"]] == [300, 310, 320]
    assert [(m["aqi"], m["stations"]) for m in body["measured"]] == [(350, 1)]
    assert [p["value"] for p in body["pollutants"]["PM2.5"]] == [150.0, 160.0]   # 9999 was invalid
    assert body["units"] == {"PM2.5": "µg/m³"}


def test_history_unknown_city_is_404(client):
    assert client.get("/api/cities/Atlantis/history").status_code == 404


def test_history_validates_hours(client):
    assert client.get("/api/cities/Delhi/history?hours=0").status_code == 422
    assert client.get("/api/cities/Delhi/history?hours=abc").status_code == 422


def test_daily_oldest_first(client):
    body = client.get("/api/cities/Delhi/daily?days=7").json()
    assert [d["date"] for d in body] == ["2026-11-05", "2026-11-06"]
    assert body[1]["pct_ok"] == 98.3 and body[1]["aqi_hours"] == 20


def test_timeline_prefers_measured_hours(client):
    body = client.get("/api/timeline?hours=6").json()
    assert len(body["times"]) == 3
    by_city = {c["location"]: c["aqi"] for c in body["cities"]}
    assert by_city == {"Delhi": [300, 310, 350], "Chennai": [60, 55, 50]}    # last Delhi hour measured


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
