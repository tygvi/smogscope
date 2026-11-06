# India AQI Live

A real-time air quality and weather pipeline for Indian cities, which will feed a live dashboard.
It ingests data from public APIs, stores it in SQLite, and computes the **official Indian
National AQI** using the CPCB method.

> **Status:** Phase 1 (data ingestion and AQI calculation) is complete. The dashboard, forecasting and satellite analysis are coming next.

## Data sources

| Source | Type | Coverage | Key needed |
|---|---|---|---|
| [CPCB via data.gov.in](https://www.data.gov.in/resource/real-time-air-quality-index-various-locations) | Measured, from about 500 ground stations | Official, but stations are clustered in big cities | Yes (free) |
| [Open-Meteo](https://open-meteo.com/) | Modeled (Copernicus CAMS) plus weather | Every location, hourly | No |

The two sources complement each other. CPCB gives accurate readings but has gaps.
Open-Meteo is complete but approximate.

## How it works

```
Open-Meteo / CPCB APIs → fetchers → SQLite (raw readings, long format)
                                  → AQI calculator → SQLite (AQI per location)
Scheduler re-runs the pipeline every 30 minutes, so history accumulates over time.
```

**Indian AQI (CPCB, 2014):**
1. Average each pollutant over its standard window: 24 hours for PM2.5, PM10, NO₂, SO₂ and NH₃; 8 hours for CO and O₃.
2. Convert each average to a sub-index (0–500) by linear interpolation between the CPCB breakpoints.
3. The AQI is the **maximum** sub-index, and that pollutant is the *dominant pollutant*.
4. The result is valid only if at least 3 pollutants are available and one of them is PM2.5 or PM10.

## Project structure

```
aqi_india/
├── config.py            # cities, paths, API key loading
├── aqi.py               # CPCB AQI calculator
├── db.py                # SQLite schema and upserts
├── pipeline.py          # one ingestion run
├── scheduler.py         # runs the pipeline every 30 min
└── fetchers/
    ├── open_meteo.py    # weather and modeled air quality
    └── cpcb.py          # official station data (with retries)
tests/test_aqi.py        # unit tests for the AQI math
```

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add your data.gov.in API key (optional)

pytest                            # run the tests
python -m aqi_india.pipeline      # one run: fetch, store, print current AQI
python -m aqi_india.scheduler     # keep collecting every 30 min
```

Without an API key, the pipeline runs on Open-Meteo alone.

## Findings so far

- **Modeled ozone is unreliable over India.** Open-Meteo/CAMS reported hourly O₃ of up to
  about 520 µg/m³ in Mumbai, which made ozone the "dominant pollutant" in most cities. That is far above
  realistic levels. The PM2.5 estimates look plausible: Delhi, Kolkata, Lucknow and Patna were above 300
  ("Very Poor") in early November. Comparing these numbers against CPCB station measurements is planned (see roadmap).
- **The government API is unreliable.** data.gov.in often hangs or returns HTTP 502, so the CPCB
  fetcher retries with exponential backoff, and the pipeline keeps running on the other source when it fails.

## Roadmap

- [x] Phase 1: ingestion, storage, CPCB AQI calculator, tests
- [ ] Phase 2: data cleaning, rolling aggregates, CPCB vs. modeled comparison
- [ ] Phase 3: Streamlit dashboard with a live India map
- [ ] Phase 4: PostgreSQL/TimescaleDB, FastAPI, Docker
- [ ] Phase 5: anomaly detection and AQI forecasting (scikit-learn baseline vs. PyTorch LSTM)
- [ ] Phase 6: satellite data (fire hotspots, smoke segmentation, PM2.5 estimates where there are no stations)
