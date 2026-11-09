-- 003: gold merging logic, as SQL functions and views (always current, nothing to keep in sync)
BEGIN;

-- AQI number -> CPCB category. The same bands as aqi_india/aqi.py CATEGORIES.
CREATE FUNCTION gold.aqi_category(aqi numeric) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN aqi IS NULL THEN NULL
        WHEN aqi <= 50  THEN 'Good'
        WHEN aqi <= 100 THEN 'Satisfactory'
        WHEN aqi <= 200 THEN 'Moderate'
        WHEN aqi <= 300 THEN 'Poor'
        WHEN aqi <= 400 THEN 'Very Poor'
        ELSE 'Severe'
    END
$$;

-- Great-circle distance in km (haversine): how far apart two lat/lon points are on Earth.
CREATE FUNCTION gold.distance_km(lat1 float8, lon1 float8, lat2 float8, lon2 float8) RETURNS float8
LANGUAGE sql IMMUTABLE AS $$
    SELECT 2 * 6371 * asin(sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2)
    ))
$$;

-- Which CPCB stations belong to which of our cities: matched by DISTANCE (<= 25 km),
-- because CPCB city names are inconsistent ("Delhi" vs "New Delhi", suburbs, ...).
CREATE VIEW gold.city_station AS
SELECT c.location_id AS city_id, c.name AS city,
       s.location_id AS station_id, s.name AS station,
       round(gold.distance_km(c.lat, c.lon, s.lat, s.lon)::numeric, 1) AS distance_km
FROM silver.location c
JOIN silver.location s ON s.kind = 'station'
WHERE c.kind = 'city'
  AND gold.distance_km(c.lat, c.lon, s.lat, s.lon) <= 25;

-- Measured AQI per city and hour: the average over its stations (as CPCB's bulletin does),
-- plus the spread, because one city's stations can range from 150 to 400.
CREATE VIEW gold.city_measured_hourly AS
SELECT cs.city_id AS location_id, a.observed_at,
       round(avg(a.aqi))::int AS aqi,
       min(a.aqi) AS min_aqi, max(a.aqi) AS max_aqi,
       count(*)   AS stations
FROM gold.city_station cs
JOIN gold.aqi_hourly a ON a.location_id = cs.station_id AND a.source = 'cpcb'
GROUP BY cs.city_id, a.observed_at;

-- The number the website shows per city: measured if a station reported in the last
-- 2 hours, otherwise the model estimate. The model-vs-measured gap is kept for ML.
CREATE VIEW gold.city_best AS
WITH model AS (
    SELECT DISTINCT ON (location_id) location_id, observed_at, aqi
    FROM gold.aqi_hourly WHERE source = 'open-meteo'
    ORDER BY location_id, observed_at DESC
), measured AS (
    SELECT DISTINCT ON (location_id) *
    FROM gold.city_measured_hourly WHERE observed_at >= now() - interval '2 hours'
    ORDER BY location_id, observed_at DESC
)
SELECT l.location_id, l.name, l.state, l.lat, l.lon,
       CASE WHEN me.aqi IS NOT NULL THEN 'measured' ELSE 'estimated' END AS basis,
       coalesce(me.aqi, mo.aqi)                        AS aqi,
       gold.aqi_category(coalesce(me.aqi, mo.aqi))     AS category,
       coalesce(me.observed_at, mo.observed_at)        AS observed_at,
       me.stations, me.min_aqi, me.max_aqi,
       me.aqi AS measured_aqi, mo.aqi AS model_aqi,
       mo.aqi - me.aqi AS model_gap
FROM silver.location l
LEFT JOIN model mo    USING (location_id)
LEFT JOIN measured me USING (location_id)
WHERE l.kind = 'city';

COMMIT;
