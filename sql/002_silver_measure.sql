-- 002: separate CPCB sub-indices from real concentrations; track silver progress
BEGIN;

-- What kind of number is in `value`?
ALTER TABLE silver.air_quality
    ADD COLUMN measure text NOT NULL DEFAULT 'concentration'
    CHECK (measure IN ('concentration', 'sub_index'));

-- measure is part of the identity of a reading
ALTER TABLE silver.air_quality DROP CONSTRAINT air_quality_pkey;
ALTER TABLE silver.air_quality
    ADD PRIMARY KEY (source, location_id, pollutant, measure, observed_at);

-- Which bronze runs have already been transformed into silver?
ALTER TABLE bronze.ingest_run ADD COLUMN silver_done_at timestamptz;

COMMIT;
