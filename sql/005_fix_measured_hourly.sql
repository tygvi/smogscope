-- 005: a station can exist in BOTH station feeds during a switch (data.gov.in + OpenAQ).
-- It is still one physical station: count it once per hour, using the freshest gold row.
-- Previously the view joined both sources and counted/averaged the station twice.
BEGIN;

CREATE OR REPLACE VIEW gold.city_measured_hourly AS
WITH freshest AS (
    SELECT DISTINCT ON (cs.city_id, a.location_id, a.observed_at)
           cs.city_id, a.location_id, a.observed_at, a.aqi
    FROM gold.city_station cs
    JOIN gold.aqi_hourly a ON a.location_id = cs.station_id
                          AND a.source IN ('cpcb', 'openaq')
    -- newest computation wins; source name breaks exact ties deterministically
    ORDER BY cs.city_id, a.location_id, a.observed_at, a.computed_at DESC, a.source DESC
)
SELECT city_id AS location_id, observed_at,
       round(avg(aqi))::int AS aqi,
       min(aqi) AS min_aqi, max(aqi) AS max_aqi,
       count(*)   AS stations
FROM freshest
GROUP BY city_id, observed_at;

COMMIT;
