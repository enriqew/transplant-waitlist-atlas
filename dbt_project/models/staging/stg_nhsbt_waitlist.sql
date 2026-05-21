{{
  config(materialized='view')
}}

-- Bronze input: data/raw/nhsbt_waitlist/<snapshot>/nhsbt_long.csv
-- (built by analyses/nhsbt_pdf_to_long.py from Table 2.1 in each annual PDF).
--
-- NHSBT uses UK fiscal years (April–March). The `year` column is the fiscal
-- year END: e.g., year=2025 means the waitlist at 31 March 2025 (fiscal
-- year 2024-2025). This differs from other sources that snapshot at 31
-- December. Downstream consumers should be aware of the ~3-month offset when
-- comparing with ET or CENATRA year-end figures.

SELECT
    'NHSBT'::VARCHAR                                        AS source,
    country_iso3,
    CAST(NULL AS VARCHAR)                                   AS region_code,
    metric_type,
    TRY_CAST(year AS INTEGER)                               AS year,
    CAST(NULL AS INTEGER)                                   AS quarter,
    CAST(NULL AS VARCHAR)                                   AS removal_reason,
    CAST(NULL AS VARCHAR)                                   AS status,
    organ,
    TRY_CAST(patients AS INTEGER)                           AS patients
FROM read_csv_auto(
    '{{ env_var("TRANSPLANT_WAITLIST_RAW_ROOT", "../data/raw") }}/nhsbt_waitlist/*/nhsbt_long.csv',
    header=true,
    ignore_errors=false
)
WHERE patients IS NOT NULL
  AND patients > 0
