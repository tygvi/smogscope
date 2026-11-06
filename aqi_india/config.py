"""Project-wide settings: which cities to track, where data lives, API keys."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")  # reads DATA_GOV_API_KEY from .env if present

DB_PATH = PROJECT_ROOT / "data" / "aqi.db"
TIMEZONE = "Asia/Kolkata"

# Empty string means "not configured" -> the CPCB fetcher is skipped.
DATA_GOV_API_KEY = os.getenv("DATA_GOV_API_KEY", "").strip()


@dataclass(frozen=True)
class City:
    name: str
    state: str
    lat: float
    lon: float


# A spread of cities across regions so the dashboard map covers all of India.
CITIES: list[City] = [
    City("Delhi", "Delhi", 28.6139, 77.2090),
    City("Mumbai", "Maharashtra", 19.0760, 72.8777),
    City("Kolkata", "West Bengal", 22.5726, 88.3639),
    City("Chennai", "Tamil Nadu", 13.0827, 80.2707),
    City("Bengaluru", "Karnataka", 12.9716, 77.5946),
    City("Hyderabad", "Telangana", 17.3850, 78.4867),
    City("Ahmedabad", "Gujarat", 23.0225, 72.5714),
    City("Pune", "Maharashtra", 18.5204, 73.8567),
    City("Jaipur", "Rajasthan", 26.9124, 75.7873),
    City("Lucknow", "Uttar Pradesh", 26.8467, 80.9462),
    City("Patna", "Bihar", 25.5941, 85.1376),
    City("Chandigarh", "Chandigarh", 30.7333, 76.7794),
    City("Guwahati", "Assam", 26.1445, 91.7362),
    City("Bhopal", "Madhya Pradesh", 23.2599, 77.4126),
    City("Thiruvananthapuram", "Kerala", 8.5241, 76.9366),
]
