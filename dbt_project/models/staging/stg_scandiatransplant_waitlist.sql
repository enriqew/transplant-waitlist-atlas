{{
  config(materialized='view')
}}

-- Bronze input: data/raw/scandiatransplant_waitlist/<snapshot>/scandiatransplant_long.csv
-- (built by analyses/scandiatransplant_pdf_to_long.py from Q4 quarterly PDFs).
--
-- Only kidney organ is available with reliable per-country subtotals. Other
-- organs (liver, heart, lung, pancreas) have center-level data only; multi-center
-- patient listings make per-country aggregation unreliable.
--
-- Year = calendar year of the Q4 report (= year-end 31 December snapshot).
-- The Q4 report for year Y is titled "January 1st, Y+1" but represents Y year-end.

SELECT
    'Scandiatransplant'::VARCHAR                             AS source,
    country_iso3,
    CAST(NULL AS VARCHAR)                                    AS region_code,
    metric_type,
    TRY_CAST(year AS INTEGER)                                AS year,
    CAST(NULL AS INTEGER)                                    AS quarter,
    CAST(NULL AS VARCHAR)                                    AS removal_reason,
    CAST(NULL AS VARCHAR)                                    AS status,
    organ,
    TRY_CAST(patients AS INTEGER)                            AS patients
FROM read_csv_auto(
    '{{ env_var("TRANSPLANT_WAITLIST_RAW_ROOT", "../data/raw") }}/scandiatransplant_waitlist/*/scandiatransplant_long.csv',
    header=true,
    ignore_errors=false
)
WHERE TRY_CAST(patients AS INTEGER) IS NOT NULL
  AND TRY_CAST(patients AS INTEGER) > 0
