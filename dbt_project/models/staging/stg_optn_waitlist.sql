{{
  config(materialized='view')
}}

-- Bronze input: data/raw/optn_waitlist/<snapshot>/optn_long.csv (built by
-- analyses/optn_pivot_to_long.py). Wide pivot CSVs from OPTN's Build Advanced
-- tool, already flattened to long format.
--
-- This staging model:
--   1. Assigns country_iso3 = 'USA' (OPTN scope is the United States).
--   2. Keeps year nullable — OPTN's current snapshot has no year axis.
--   3. Maps every column to the unified waitlist contract that the gold marts
--      union from across CENATRA / Eurotransplant / OPTN.

SELECT
    'OPTN'::VARCHAR                                        AS source,
    'USA'::VARCHAR                                         AS country_iso3,
    CAST(NULL AS VARCHAR)                                  AS region_code,
    metric_type,
    TRY_CAST(year AS INTEGER)                              AS year,
    CAST(NULL AS INTEGER)                                  AS quarter,
    NULLIF(removal_reason, '')                             AS removal_reason,
    NULLIF(status, '')                                     AS status,
    organ,
    TRY_CAST(patients AS INTEGER)                          AS patients
FROM read_csv_auto(
    '{{ env_var("TRANSPLANT_WAITLIST_RAW_ROOT", "../data/raw") }}/optn_waitlist/*/optn_long.csv',
    header=true,
    ignore_errors=false
)
WHERE patients IS NOT NULL
  AND patients > 0
