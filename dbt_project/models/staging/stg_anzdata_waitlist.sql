{{
  config(materialized='view')
}}

-- Bronze input: data/raw/anzdata_waitlist/<snapshot>/anzdata_long.csv
-- (built by analyses/anzdata_xlsx_to_long.py from Chapter 6 Excel files).
--
-- ANZDATA Chapter 6 covers the Australian kidney transplant waiting list only.
-- "Active end of year" = active patients at 31 December (calendar year-end).
-- New Zealand and other organs are not covered in this chapter.

SELECT
    'ANZDATA'::VARCHAR                                       AS source,
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
    '{{ env_var("TRANSPLANT_WAITLIST_RAW_ROOT", "../data/raw") }}/anzdata_waitlist/*/anzdata_long.csv',
    header=true,
    ignore_errors=false
)
WHERE TRY_CAST(patients AS INTEGER) IS NOT NULL
  AND TRY_CAST(patients AS INTEGER) > 0
