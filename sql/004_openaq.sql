-- 004: OpenAQ as a second way to receive CPCB station data.
--
-- data.gov.in refuses connections from outside India (Q47), so a server abroad gets the
-- same CPCB stations through OpenAQ instead. OpenAQ sends CONCENTRATIONS (µg/m³), not
-- the sub-indices data.gov.in sends (Q40), so its readings go through our own AQI code.
--
-- Applying it to an EXISTING database (Docker only runs sql/ on an empty volume):
--   docker compose exec -T db psql -U smogscope -d smogscope -v ON_ERROR_STOP=1 < sql/004_openaq.sql
BEGIN;

-- 1. New source name allowed in the audit log and in silver.
--    (The CHECKs were declared inline in 001, so PostgreSQL named them <table>_<column>_check.)
ALTER TABLE bronze.ingest_run DROP CONSTRAINT ingest_run_source_check;
ALTER TABLE bronze.ingest_run ADD CONSTRAINT ingest_run_source_check
    CHECK (source IN ('open-meteo-aq', 'open-meteo-weather', 'cpcb', 'openaq'));

ALTER TABLE silver.air_quality DROP CONSTRAINT air_quality_source_check;
ALTER TABLE silver.air_quality ADD CONSTRAINT air_quality_source_check
    CHECK (source IN ('open-meteo', 'cpcb', 'openaq'));

-- 2. Bronze: one flat JSON record per (station sensor, latest reading), like bronze.cpcb_record.
CREATE TABLE bronze.openaq_record (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      bigint      NOT NULL REFERENCES bronze.ingest_run,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    record      jsonb       NOT NULL
);
CREATE INDEX ON bronze.openaq_record (run_id);

-- 3. A city's measured AQI may now come from either station feed.
--    CREATE OR REPLACE keeps gold.city_best (which depends on this view) intact,
--    because the columns stay exactly the same.
CREATE OR REPLACE VIEW gold.city_measured_hourly AS
SELECT cs.city_id AS location_id, a.observed_at,
       round(avg(a.aqi))::int AS aqi,
       min(a.aqi) AS min_aqi, max(a.aqi) AS max_aqi,
       count(*)   AS stations
FROM gold.city_station cs
JOIN gold.aqi_hourly a ON a.location_id = cs.station_id AND a.source IN ('cpcb', 'openaq')
GROUP BY cs.city_id, a.observed_at;

COMMIT;
