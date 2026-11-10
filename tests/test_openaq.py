"""Tests for the OpenAQ route: unit conversion, the fetch logic (with a fake API),
and city names. No network: the HTTP client is replaced."""
import pytest

from aqi_india.fetchers import openaq
from aqi_india.silver import _openaq_city


# --- units ----------------------------------------------------------------------

@pytest.mark.parametrize("pollutant, value, units, expected", [
    ("pm25", 85.0, "µg/m³", 85.0),
    ("pm25", 85.0, "μg/m³", 85.0),          # Greek mu instead of the micro sign
    ("pm10", 120.0, "ug/m3", 120.0),        # ASCII spelling
    ("co", 1500.0, "µg/m³", 1.5),           # CPCB's CO unit is mg/m³
    ("co", 1.5, "mg/m³", 1.5),
    ("co", 1.0, "ppm", 28.01 / 24.45),      # 1 ppm CO ≈ 1.146 mg/m³
    ("no2", 100.0, "ppb", 100 * 46.01 / 24.45),
    ("o3", 0.05, "ppm", 50 * 48.00 / 24.45),
])
def test_to_cpcb_units(pollutant, value, units, expected):
    assert openaq.to_cpcb_units(pollutant, value, units) == pytest.approx(expected)


def test_unknown_units_are_rejected_not_guessed():
    assert openaq.to_cpcb_units("pm25", 10.0, "particles/cm³") is None
    assert openaq.to_cpcb_units("pm25", 10.0, "ppm") is None    # no molar mass for particles
    assert openaq.to_cpcb_units("pm25", None, "µg/m³") is None


# --- fetch logic, against a fake API ----------------------------------------------

LOCATIONS = [
    {"id": 1, "name": "Anand Vihar, Delhi - DPCC", "locality": None, "isMobile": False,
     "provider": {"name": "CPCB"}, "coordinates": {"latitude": 28.65, "longitude": 77.31},
     "sensors": [{"id": 11, "parameter": {"id": 2, "name": "pm25", "units": "µg/m³"}},
                 {"id": 12, "parameter": {"id": 4, "name": "co", "units": "µg/m³"}},
                 {"id": 13, "parameter": {"id": 99, "name": "temperature", "units": "c"}}]},
    {"id": 2, "name": "Some Sensor", "isMobile": False, "provider": {"name": "AirGradient"},
     "coordinates": {"latitude": 28.6, "longitude": 77.2},
     "sensors": [{"id": 21, "parameter": {"id": 2, "name": "pm25", "units": "µg/m³"}}]},
]
LATEST = {
    2: [{"sensorsId": 11, "value": 310.0, "datetime": {"utc": "2026-11-10T04:30:00Z"}},
        {"sensorsId": 21, "value": 999.0, "datetime": {"utc": "2026-11-10T04:30:00Z"}},     # low-cost: dropped
        {"sensorsId": 555, "value": 12.0, "datetime": {"utc": "2026-11-10T04:30:00Z"}}],    # other country
    4: [{"sensorsId": 12, "value": 2100.0, "datetime": {"utc": "2026-11-10T04:30:00Z"}}],
}


class FakeClient:
    def __init__(self, api_key):
        self.calls = 0
        self.paths = []

    def pages(self, path, params):
        self.paths.append(path)
        self.calls += 1
        if path == "/locations":
            assert params == {"iso": "IN"}
            return iter(LOCATIONS)
        return iter(LATEST[int(path.split("/")[2])])


def test_fetch_keeps_only_cpcb_pollutant_sensors(monkeypatch):
    monkeypatch.setattr(openaq, "_Client", FakeClient)
    records = openaq.fetch_records("key")

    assert {(r["sensor_id"], r["parameter"], r["value"]) for r in records} == {(11, "pm25", 310.0), (12, "co", 2100.0)}
    r = records[0]
    assert r["station"] == "Anand Vihar, Delhi - DPCC"
    assert (r["latitude"], r["longitude"]) == (28.65, 77.31)
    assert r["datetime_utc"] == "2026-11-10T04:30:00Z"


def test_fetch_asks_once_per_parameter_not_per_station(monkeypatch):
    seen = {}

    class Recording(FakeClient):
        def __init__(self, api_key):
            super().__init__(api_key)
            seen["client"] = self

    monkeypatch.setattr(openaq, "_Client", Recording)
    openaq.fetch_records("key")
    # 1 locations listing + 1 per pollutant parameter (pm25, co); temperature is never requested
    assert seen["client"].paths == ["/locations", "/parameters/2/latest", "/parameters/4/latest"]


def test_no_fresh_readings_is_an_error(monkeypatch):
    monkeypatch.setattr(openaq, "_Client", FakeClient)
    monkeypatch.setitem(LATEST, 2, [])
    monkeypatch.setitem(LATEST, 4, [])
    with pytest.raises(openaq.OpenAQError, match="no CPCB readings"):
        openaq.fetch_records("key")


def test_missing_key_fails_fast():
    with pytest.raises(openaq.OpenAQError, match="OPENAQ_API_KEY"):
        openaq._Client("")


# --- city names -------------------------------------------------------------------

@pytest.mark.parametrize("record, city", [
    ({"locality": "Delhi", "station": "Anand Vihar, Delhi - DPCC"}, "Delhi"),
    ({"locality": None, "station": "Anand Vihar, Delhi - DPCC"}, "Delhi"),
    ({"locality": None, "station": "Bandra Kurla Complex, Mumbai - IITM"}, "Mumbai"),
    ({"locality": None, "station": "NoCommaHere"}, ""),
])
def test_openaq_city(record, city):
    assert _openaq_city(record) == city
