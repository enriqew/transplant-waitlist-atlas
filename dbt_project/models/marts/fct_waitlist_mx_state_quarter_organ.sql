{{
  config(materialized='table')
}}

-- Mexico drill-down: keeps the full quarterly × state × organ granularity that
-- CENATRA publishes. Feeds the Mexico inset map and the per-state sidebar.

SELECT
    country_iso3,
    region_code,
    year,
    quarter,
    organ,
    SUM(patients)  AS patients,
    source
FROM {{ ref('stg_cenatra_waitlist') }}
WHERE metric_type = 'stock'
GROUP BY 1, 2, 3, 4, 5, 7
