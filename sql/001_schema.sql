-- SmogScope warehouse: medallion layers  bronze (raw) -> silver (clean) -> gold (results)
BEGIN;

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

-- =====================================================================
-- BRONZE: exactly what each API returned. Append-only, never edited.
-- =====================================================================

-- One row per fetch attempt (success or failure): our audit log.
CREATE TABLE bronze.ingest_run (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text        NOT NULL CHECK (source IN ('open-meteo-aq', 'open-meteo-weather', 'cpcb')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text        NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
    records      integer,
    error        text
);

-- Open-Meteo returns one JSON document per city: store it whole.
CREATE TABLE bronze.open_meteo_air_quality (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      bigint      NOT NULL REFERENCES bronze.ingest_run,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    location    text        NOT NULL,
    payload     jsonb       NOT NULL
);

CREATE TABLE bronze.open_meteo_weather (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      bigint      NOT NULL REFERENCES bronze.ingest_run,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    location    text        NOT NULL,
    payload     jsonb       NOT NULL
);

-- CPCB returns ~4,000 flat records per fetch: one row per record.
CREATE TABLE bronze.cpcb_record (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      bigint      NOT NULL REFERENCES bronze.ingest_run,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    record      jsonb       NOT NULL
);

CREATE INDEX ON bronze.open_meteo_air_quality (run_id);
CREATE INDEX ON bronze.open_meteo_weather (run_id);
CREATE INDEX ON bronze.cpcb_record (run_id);

-- =====================================================================
-- SILVER: cleaned, typed, de-duplicated. Both sources in one shape.
-- =====================================================================

CREATE TYPE silver.quality_flag AS ENUM ('ok', 'suspect', 'invalid');

-- Reference table: the only pollutant codes allowed anywhere.
CREATE TABLE silver.pollutant (
    code       text     PRIMARY KEY,
    name       text     NOT NULL,
    unit       text     NOT NULL,
    avg_hours  smallint NOT NULL          -- CPCB averaging window: 24 h, or 8 h for CO/O3
);
INSERT INTO silver.pollutant VALUES
    ('pm25', 'PM2.5', 'µg/m³', 24), ('pm10', 'PM10', 'µg/m³', 24),
    ('no2',  'NO₂',   'µg/m³', 24), ('so2',  'SO₂',  'µg/m³', 24),
    ('nh3',  'NH₃',   'µg/m³', 24), ('co',   'CO',   'mg/m³',  8),
    ('o3',   'O₃',    'µg/m³',  8);

-- Every place we have data for: our 15 cities AND each CPCB station.
CREATE TABLE silver.location (
    location_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('city', 'station')),
    name         text NOT NULL,           -- 'Delhi'  or  'Urja Nagar, Korba - CECB'
    city         text NOT NULL,
    state        text NOT NULL,
    lat          double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon          double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
    UNIQUE (kind, name)
);

CREATE TABLE silver.air_quality (
    source       text NOT NULL CHECK (source IN ('open-meteo', 'cpcb')),
    location_id  integer NOT NULL REFERENCES silver.location,
    pollutant    text    NOT NULL REFERENCES silver.pollutant,
    observed_at  timestamptz NOT NULL,
    value        double precision,                       -- NULL when the source said "NA"
    flag         silver.quality_flag NOT NULL DEFAULT 'ok',
    flag_reason  text,
    run_id       bigint REFERENCES bronze.ingest_run,    -- lineage: which fetch produced this
    PRIMARY KEY (source, location_id, pollutant, observed_at)
);
CREATE INDEX ON silver.air_quality (observed_at);

CREATE TABLE silver.weather (
    source            text NOT NULL DEFAULT 'open-meteo',
    location_id       integer NOT NULL REFERENCES silver.location,
    observed_at       timestamptz NOT NULL,
    temperature_c     real,
    humidity_pct      real CHECK (humidity_pct BETWEEN 0 AND 100),
    wind_speed_kmh    real CHECK (wind_speed_kmh >= 0),
    wind_dir_deg      real CHECK (wind_dir_deg BETWEEN 0 AND 360),
    pressure_hpa      real,
    precipitation_mm  real CHECK (precipitation_mm >= 0),
    cloud_cover_pct   real CHECK (cloud_cover_pct BETWEEN 0 AND 100),
    run_id            bigint REFERENCES bronze.ingest_run,
    PRIMARY KEY (source, location_id, observed_at)
);

-- =====================================================================
-- GOLD: results for the website and ML. Rebuildable from silver.
-- =====================================================================

CREATE TABLE gold.aqi_hourly (
    source       text NOT NULL,
    location_id  integer NOT NULL REFERENCES silver.location,
    observed_at  timestamptz NOT NULL,
    aqi          smallint NOT NULL CHECK (aqi >= 0),
    category     text NOT NULL CHECK (category IN
                   ('Good', 'Satisfactory', 'Moderate', 'Poor', 'Very Poor', 'Severe')),
    dominant     text REFERENCES silver.pollutant,
    pm25_si smallint, pm10_si smallint, no2_si smallint, so2_si smallint,
    nh3_si  smallint, co_si   smallint, o3_si  smallint,     -- sub-indices
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, location_id, observed_at)
);

CREATE TABLE gold.daily_summary (
    source          text NOT NULL,
    location_id     integer NOT NULL REFERENCES silver.location,
    day             date NOT NULL,
    mean_aqi        real,
    min_aqi         smallint,
    max_aqi         smallint,
    worst_category  text,
    hours_covered   smallint CHECK (hours_covered BETWEEN 0 AND 24),
    pct_ok          real     CHECK (pct_ok BETWEEN 0 AND 100),
    PRIMARY KEY (source, location_id, day)
);

CREATE TABLE gold.daily_pollutant (
    source       text NOT NULL,
    location_id  integer NOT NULL REFERENCES silver.location,
    day          date NOT NULL,
    pollutant    text NOT NULL REFERENCES silver.pollutant,
    min_value    double precision,
    mean_value   double precision,
    max_value    double precision,
    n_readings   smallint,
    PRIMARY KEY (source, location_id, day, pollutant)
);

-- Latest AQI per place: what the map shows. A view, so it is never stale.
CREATE VIEW gold.city_latest AS
SELECT DISTINCT ON (a.source, a.location_id)
       a.source, l.name, l.city, l.state, l.lat, l.lon,
       a.observed_at, a.aqi, a.category, a.dominant
FROM gold.aqi_hourly a
JOIN silver.location l USING (location_id)
ORDER BY a.source, a.location_id, a.observed_at DESC;

COMMIT;
