-- 02_quality.sql: field availability for the sample and data-integrity checks on parsed values.
--
-- Statuses come from src/prepare.py and are never rewritten. Each is grouped into one availability
-- class so that withheld, not-yet-available, blank and needs-review cells stay distinct; none counts
-- as zero or as a value.

CREATE OR REPLACE TABLE availability_classes AS
SELECT * FROM (VALUES
    ('parsed', 'available'),
    ('present', 'available'),
    ('blank', 'blank_in_source'),
    ('exempt', 'withheld_foi_exemption'),
    ('planning_stage', 'not_yet_available_planning_stage'),
    ('range', 'range_no_scalar_value'),
    ('ambiguous_day_month', 'needs_review'),
    ('partial_date', 'needs_review'),
    ('invalid_date', 'needs_review'),
    ('unverified_date_format', 'needs_review'),
    ('unrecognised', 'needs_review'),
    ('not_collected_in_release', 'not_collected_in_release')
) t(status, availability_class);

CREATE OR REPLACE TABLE field_availability AS
WITH denominators AS (
    SELECT release_year, count(*) AS sample_observations FROM sample_observations GROUP BY release_year
)
SELECT f.release_year,
       f.canonical_field,
       f.output_field,
       f.status,
       a.availability_class,
       count(*) AS observations_with_status,
       d.sample_observations,
       round(100.0 * count(*) / d.sample_observations, 1) AS share_pct
FROM src_field_status f
JOIN sample_observations s ON s.observation_id = f.observation_id
JOIN denominators d ON d.release_year = f.release_year
LEFT JOIN availability_classes a ON a.status = f.status
GROUP BY f.release_year, f.canonical_field, f.output_field, f.status, a.availability_class, d.sample_observations;

-- Checks -------------------------------------------------------------------------------------------

INSERT INTO sql_checks
SELECT 'Q01', '02_quality', 'every sample observation has one status row per mapped field of its release',
       actual, expected, actual = expected
FROM (
    WITH per_obs AS (
        SELECT s.observation_id, s.release_year, count(f.observation_id) AS n
        FROM sample_observations s
        LEFT JOIN src_field_status f ON f.observation_id = s.observation_id
        GROUP BY ALL
    ),
    per_release AS (
        SELECT release_year, count(DISTINCT output_field) AS m FROM src_field_status GROUP BY ALL
    )
    SELECT count(*) AS actual, 0 AS expected
    FROM per_obs p LEFT JOIN per_release r ON r.release_year = p.release_year
    WHERE r.m IS NULL OR p.n <> r.m
);

INSERT INTO sql_checks
SELECT 'Q02', '02_quality', 'every field status has an availability class', actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected
      FROM (SELECT DISTINCT status FROM src_field_status) f
      LEFT JOIN availability_classes a ON a.status = f.status
      WHERE a.status IS NULL);

INSERT INTO sql_checks
SELECT 'Q03', '02_quality', 'availability counts sum to the sample denominator for each release and field',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM (
    SELECT release_year, output_field, sum(observations_with_status) AS total, any_value(sample_observations) AS d
    FROM field_availability GROUP BY ALL HAVING sum(observations_with_status) <> any_value(sample_observations)));

INSERT INTO sql_checks
SELECT 'Q04', '02_quality', 'every parsed start and end date is a valid calendar date', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM src_observations
      WHERE (start_date_status = 'parsed' AND TRY_CAST(start_date AS DATE) IS NULL)
         OR (end_date_status = 'parsed' AND TRY_CAST(end_date AS DATE) IS NULL));

INSERT INTO sql_checks
SELECT 'Q05', '02_quality', 'every parsed DCA rating is GREEN, AMBER or RED', actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM src_observations
      WHERE (external_dca_status = 'parsed' AND external_dca NOT IN ('GREEN', 'AMBER', 'RED'))
         OR (sro_dca_status = 'parsed' AND sro_dca NOT IN ('GREEN', 'AMBER', 'RED')));

INSERT INTO sql_checks
SELECT 'Q06', '02_quality', 'no observation has ratings in both the external and SRO DCA fields', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM src_observations
      WHERE external_dca_status = 'parsed' AND sro_dca_status = 'parsed');

INSERT INTO sql_checks
SELECT 'Q07', '02_quality', 'every parsed amount has at most 6 decimal places (exact in DECIMAL(38,6))',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM src_observations
      WHERE (fy_baseline_gbp_m_status = 'parsed' AND regexp_matches(fy_baseline_gbp_m, '\.\d{7,}'))
         OR (fy_forecast_gbp_m_status = 'parsed' AND regexp_matches(fy_forecast_gbp_m, '\.\d{7,}')));

INSERT INTO sql_checks
SELECT 'Q08', '02_quality', 'every parsed amount and published percentage casts to a number', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM src_observations
      WHERE (fy_baseline_gbp_m_status = 'parsed' AND TRY_CAST(fy_baseline_gbp_m AS DECIMAL(38, 6)) IS NULL)
         OR (fy_forecast_gbp_m_status = 'parsed' AND TRY_CAST(fy_forecast_gbp_m AS DECIMAL(38, 6)) IS NULL)
         OR (fy_variance_pct_status = 'parsed' AND TRY_CAST(fy_variance_pct AS DOUBLE) IS NULL));
