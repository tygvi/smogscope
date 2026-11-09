"""One full warehouse refresh: bronze (fetch raw) -> silver (clean) -> gold (AQI).

Run once:  python -m aqi_india.warehouse
The scheduler calls run_warehouse() every 30 minutes.

Each stage tolerates the previous one partly failing: if CPCB is down, bronze
still stores Open-Meteo, silver transforms whatever new runs exist, and gold
recomputes from everything in silver.
"""
import logging

from .gold import run_gold
from .pg import ingest_all
from .silver import run_silver

log = logging.getLogger(__name__)


def run_warehouse() -> None:
    ingest_all()     # per-source failures are logged in bronze.ingest_run, never raised
    run_silver()     # per-run failures are rolled back and retried next time
    run_gold()       # a failure here raises: the scheduler logs it and tries again later


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    run_warehouse()
