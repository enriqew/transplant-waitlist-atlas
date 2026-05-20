{{
  config(materialized='view')
}}

-- Bronze input: data/raw/cenatra_waitlist__pacientes_espera_organo_tejido/<snapshot>/cenatra_long.csv
-- (built by analyses/cenatra_waitlist_to_long.py from 22 quarterly patient-level
-- CSVs aggregated by year × quarter × state × organ).
--
-- CENATRA scope is México only; the parser sets country_iso3='MEX' and
-- region_code to the ISO 3166-2:MX state 3-letter code (AGU, BCN, ..., ZAC).
-- All CENATRA rows are end-of-quarter stock snapshots; there is no removal_reason
-- or status — leave those NULL in the unified contract.

SELECT
    'CENATRA'::VARCHAR                                     AS source,
    country_iso3,
    region_code,
    metric_type,
    TRY_CAST(year AS INTEGER)                              AS year,
    TRY_CAST(quarter AS INTEGER)                           AS quarter,
    CAST(NULL AS VARCHAR)                                  AS removal_reason,
    CAST(NULL AS VARCHAR)                                  AS status,
    organ,
    TRY_CAST(patients AS INTEGER)                          AS patients
FROM read_csv_auto(
    '{{ env_var("TRANSPLANT_WAITLIST_RAW_ROOT", "../data/raw") }}/cenatra_waitlist__pacientes_espera_organo_tejido/*/cenatra_long.csv',
    header=true,
    ignore_errors=false
)
WHERE patients IS NOT NULL
  AND patients > 0
