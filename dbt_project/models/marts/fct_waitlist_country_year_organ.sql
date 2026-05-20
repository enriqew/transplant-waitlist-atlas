{{
  config(materialized='table')
}}

-- World view: one row per (country, year, organ) of "patients on the waiting
-- list at the end of that year". This is the headline fact feeding the
-- portfolio choropleth.
--
-- Source rules:
--   * OPTN — stock rows only (year IS NOT NULL means it's a historical year-end
--     snapshot, which OPTN doesn't actually publish; the current-snapshot rows
--     have NULL year and are filtered out here).
--   * Eurotransplant — stock rows (the parser only emits 'stock' for now).
--     Already at country × year × organ granularity, no aggregation needed.
--   * CENATRA — stock rows, aggregated across quarters. We take Q4 (year-end)
--     as the canonical "year-end snapshot" — comparable to ET's year-end
--     definition. Other quarters are still available in stg_cenatra_waitlist
--     for the Mexico drill-down mart.

WITH optn_stock AS (
    -- OPTN historical stock by year does not exist in the current snapshot file;
    -- we keep this branch ready for when SRTR Annual Reports get ingested.
    SELECT country_iso3, year, organ, SUM(patients) AS patients, source
    FROM {{ ref('stg_optn_waitlist') }}
    WHERE metric_type = 'stock' AND year IS NOT NULL
    GROUP BY 1, 2, 3, 5
),

eurotransplant_stock AS (
    SELECT country_iso3, year, organ, SUM(patients) AS patients, source
    FROM {{ ref('stg_eurotransplant_waitlist') }}
    WHERE metric_type = 'stock'
    GROUP BY 1, 2, 3, 5
),

cenatra_year_end AS (
    -- Q4 of each year is the canonical year-end snapshot for comparability.
    SELECT country_iso3, year, organ, SUM(patients) AS patients, source
    FROM {{ ref('stg_cenatra_waitlist') }}
    WHERE metric_type = 'stock' AND quarter = 4
    GROUP BY 1, 2, 3, 5
)

SELECT country_iso3, year, organ, patients, source FROM optn_stock
UNION ALL
SELECT country_iso3, year, organ, patients, source FROM eurotransplant_stock
UNION ALL
SELECT country_iso3, year, organ, patients, source FROM cenatra_year_end
