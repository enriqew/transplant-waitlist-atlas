{{
  config(materialized='view')
}}

-- Bronze input: data/raw/ont_waitlist/<snapshot>/ont_long.csv
-- (built by analyses/ont_pdf_to_long.py from the "Activo" year-end series in
-- each ONT organ activity PDF, and from the renal year-end waitlist chart).
--
-- ONT uses calendar years; snapshot at 31 December. The `year` column is the
-- calendar year of the snapshot (e.g. year=2024 = active list at 31 Dec 2024).
-- This differs from NHSBT which uses fiscal year-end (31 March).

SELECT
    'ONT'::VARCHAR                                            AS source,
    country_iso3,
    CAST(NULL AS VARCHAR)                                     AS region_code,
    metric_type,
    TRY_CAST(year AS INTEGER)                                 AS year,
    CAST(NULL AS INTEGER)                                     AS quarter,
    CAST(NULL AS VARCHAR)                                     AS removal_reason,
    CAST(NULL AS VARCHAR)                                     AS status,
    organ,
    TRY_CAST(patients AS INTEGER)                             AS patients
FROM read_csv_auto(
    '{{ env_var("TRANSPLANT_WAITLIST_RAW_ROOT", "../data/raw") }}/ont_waitlist/*/ont_long.csv',
    header=true,
    ignore_errors=false
)
WHERE patients IS NOT NULL
  AND patients > 0
