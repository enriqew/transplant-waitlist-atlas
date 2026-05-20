{{
  config(materialized='view')
}}

-- Bronze input: data/raw/eurotransplant_waitlist/<snapshot>/eurotransplant_long.csv
-- (built by analyses/eurotransplant_xlsx_to_long.py from XLSX report 10728-33135).
--
-- Eurotransplant covers 8 member countries plus occasional ITA cross-zone
-- exchange rows. The parser already mapped country names → ISO3 and organ
-- labels → canonical codes. This model normalizes to the unified contract.

SELECT
    'Eurotransplant'::VARCHAR                              AS source,
    country_iso3,
    CAST(NULL AS VARCHAR)                                  AS region_code,
    metric_type,
    TRY_CAST(year AS INTEGER)                              AS year,
    CAST(NULL AS INTEGER)                                  AS quarter,
    NULLIF(removal_reason, '')                             AS removal_reason,
    NULLIF(status, '')                                     AS status,
    organ,
    TRY_CAST(patients AS INTEGER)                          AS patients
FROM read_csv_auto(
    '{{ env_var("TRANSPLANT_WAITLIST_RAW_ROOT", "../data/raw") }}/eurotransplant_waitlist/*/eurotransplant_long.csv',
    header=true,
    ignore_errors=false
)
WHERE patients IS NOT NULL
  AND patients >= 0  -- ET legitimately reports 0 for "organ not offered this year"
