{{
  config(materialized='table')
}}

-- Fate distribution: for each (country, year, organ), how do patients leave
-- the waiting list? This is the gold mart that powers the differentiating
-- visualization — "of N people who entered the US waitlist in 2018, X% were
-- transplanted (deceased/living), Y% died waiting, Z% were removed for medical
-- improvement, ...".
--
-- Sources:
--   * OPTN — removals with explicit removal_reason (21 distinct values).
--   * Eurotransplant — removals are in separate per-organ XLSX reports
--     (11146-*); parser support pending. Branch kept ready.
--   * CENATRA — does not publish removal reasons; only stock snapshots.
--     Difference-of-stocks across quarters gives a proxy for removals but
--     without reason. Out of scope for this mart.
--
-- Normalized removal reason taxonomy (keeps the silver detail but tags each
-- raw label with one of six analytic buckets that can be UNION-ed across
-- sources later):
--   transplanted_deceased | transplanted_living | died_waiting |
--   removed_too_sick      | improved            | other

WITH optn_removals AS (
    SELECT
        country_iso3,
        year,
        organ,
        removal_reason,
        CASE
            WHEN removal_reason ILIKE '%Deceased Donor Transplant%'
              OR removal_reason ILIKE '%Deceased Donor Emergency%'
              OR removal_reason ILIKE '%Deceased Donor Multi-Organ%'
              OR removal_reason ILIKE '%Transplanted At Another Center%'
              OR removal_reason ILIKE '%Transplanted in another country%' THEN 'transplanted_deceased'
            WHEN removal_reason ILIKE '%Living Donor Transplant%'      THEN 'transplanted_living'
            WHEN removal_reason ILIKE '%Died%'                          THEN 'died_waiting'
            WHEN removal_reason ILIKE '%Patient died during TX procedure%' THEN 'died_waiting'
            WHEN removal_reason ILIKE '%Too Sick to Transplant%'        THEN 'removed_too_sick'
            WHEN removal_reason ILIKE '%Medically Unsuitable%'          THEN 'removed_too_sick'
            WHEN removal_reason ILIKE '%Condition Improved%'            THEN 'improved'
            WHEN removal_reason ILIKE '%Refused%'                       THEN 'other'
            WHEN removal_reason ILIKE '%Transferred to another center%' THEN 'other'
            WHEN removal_reason ILIKE '%Unable to contact%'             THEN 'other'
            WHEN removal_reason ILIKE '%Other%'                         THEN 'other'
            ELSE 'other'
        END                                                              AS reason_bucket,
        SUM(patients)                                                    AS patients,
        source
    FROM {{ ref('stg_optn_waitlist') }}
    WHERE metric_type = 'removals' AND year IS NOT NULL
    GROUP BY 1, 2, 3, 4, 5, 7
)

SELECT
    country_iso3,
    year,
    organ,
    reason_bucket,
    removal_reason                                                       AS removal_reason_raw,
    patients,
    source
FROM optn_removals
