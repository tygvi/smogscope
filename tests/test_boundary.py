"""The map's India boundary must follow the official (Survey of India) depiction.

Guards web/public/india-boundary.geojson against being replaced by, or
regenerated from, a dataset that uses de facto (OSM-style) borders.
"""
import json

import pytest

from aqi_india.config import PROJECT_ROOT

BOUNDARY = PROJECT_ROOT / "web" / "public" / "india-boundary.geojson"


def _polygons():
    geometry = json.loads(BOUNDARY.read_text())["features"][0]["geometry"]
    assert geometry["type"] == "MultiPolygon"
    return geometry["coordinates"]


def _inside(point, polygons) -> bool:
    """Ray casting: count how many polygon edges a ray to the east crosses."""
    x, y = point
    hit = False
    for polygon in polygons:
        for ring in polygon:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                    hit = not hit
    return hit


@pytest.mark.parametrize("name, lon, lat", [
    ("New Delhi", 77.21, 28.61),
    ("Srinagar", 74.80, 34.08),
    ("Leh", 77.58, 34.15),
    ("Muzaffarabad (PoK)", 73.47, 34.37),
    ("Gilgit (Gilgit-Baltistan)", 74.31, 35.92),
    ("Aksai Chin", 79.50, 35.20),
    ("Tawang (Arunachal Pradesh)", 91.86, 27.59),
    ("Port Blair (Andaman)", 92.73, 11.62),
])
def test_inside_official_boundary(name, lon, lat):
    assert _inside((lon, lat), _polygons()), f"{name} should be inside India's official boundary"


@pytest.mark.parametrize("name, lon, lat", [
    ("Lahore", 74.34, 31.55),
    ("Islamabad", 73.05, 33.68),
    ("Kathmandu", 85.32, 27.71),
    ("Dhaka", 90.41, 23.81),
    ("Colombo", 79.86, 6.93),
])
def test_outside_official_boundary(name, lon, lat):
    assert not _inside((lon, lat), _polygons()), f"{name} should be outside India"


def test_northernmost_point_is_in_gilgit_baltistan():
    points = [p for poly in _polygons() for ring in poly for p in ring]
    lon, lat = max(points, key=lambda p: p[1])
    assert lat > 37.0 and 74 < lon < 76   # official northern tip, ~37°05'N


def test_file_is_small_enough_for_the_web():
    assert BOUNDARY.stat().st_size < 150_000
