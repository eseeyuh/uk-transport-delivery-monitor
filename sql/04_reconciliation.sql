-- 04_reconciliation.sql: calculated annual variance percentage against the published
-- "Financial Year Variance (%)" column, and coverage counts.
--
-- Sign convention (investigated 22 October 2026): the published column holds unsigned magnitudes.
-- In both releases no published value is negative, and for every observation with parsed baseline,
-- forecast and published value and a positive baseline, |calculated| matched the published value
-- within 0.1 pp in 320 of 321 cases, including forecasts below baseline. The comparison therefore uses
-- |calculated|; the signed calculated value is kept unchanged. Check R01 stops the build if a
-- negative published value ever appears, because the convention would then need review.
-- Tolerance: params.variance_tolerance_pp (0.1 percentage point, methodology section 4.3), plus a
-- floating-point epsilon. Differences beyond it are flagged for investigation; neither value is
-- overwritten.

CREATE OR REPLACE TABLE variance_reconciliation_all AS
WITH joined AS (
    SELECT v.observation_id,
           v.release_year,
           v.source_file,
           v.source_row,
           v.in_sample,
           v.variance_pct AS calculated_pct,
           v.variance_pct_status,
           o.fy_variance_pct_raw AS published_raw,
           o.fy_variance_pct_status AS published_status,
           CASE WHEN o.fy_variance_pct_status = 'parsed' THEN CAST(o.fy_variance_pct AS DOUBLE) END AS published_pct,
           p.variance_tolerance_pp AS tolerance_pp,
           p.comparison_epsilon
    FROM annual_variance_all v
    JOIN src_observations o ON o.observation_id = v.observation_id
    CROSS JOIN params p
)
SELECT * EXCLUDE (comparison_epsilon),
       'published value is an unsigned magnitude; compared with |calculated|' AS comparison_basis,
       abs(calculated_pct) - published_pct AS difference_pp,
       CASE WHEN calculated_pct IS NULL THEN 'not_compared_calculated_unavailable'
            WHEN published_status <> 'parsed' THEN 'not_compared_published_' || published_status
            WHEN abs(abs(calculated_pct) - published_pct) <= tolerance_pp + comparison_epsilon THEN 'within_tolerance'
            ELSE 'outside_tolerance_investigate' END AS reconciliation_status
FROM joined;

CREATE OR REPLACE TABLE variance_reconciliation AS
SELECT * FROM variance_reconciliation_all WHERE in_sample;

-- Coverage of the sample and of each comparison. Counts are observations or confirmed pairs (one per
-- project), never candidate rows. For each comparison the eligible pairs and the excluded pairs (one
-- reason each) add up to the confirmed pairs, and only eligible pairs are split into outcomes: a
-- missing or ineligible comparison is reported as excluded, never as unchanged.
CREATE OR REPLACE TABLE coverage AS
WITH sched AS (SELECT * FROM schedule_comparison), dca AS (SELECT * FROM dca_comparison)
SELECT * FROM (
    SELECT 1 AS sort_order, 'sample' AS measure_group, 'included_observations' AS measure,
           r.old_release AS release_year,
           (SELECT count(*) FROM sample_observations s WHERE s.release_year = r.old_release) AS n FROM releases r
    UNION ALL
    SELECT 2, 'sample', 'included_observations', r.new_release,
           (SELECT count(*) FROM sample_observations s WHERE s.release_year = r.new_release) FROM releases r
    UNION ALL
    SELECT 3, 'sample', 'included_observations_without_confirmed_pair', r.old_release,
           (SELECT count(*) FROM unmatched_observations u WHERE u.release_year = r.old_release) FROM releases r
    UNION ALL
    SELECT 4, 'sample', 'included_observations_without_confirmed_pair', r.new_release,
           (SELECT count(*) FROM unmatched_observations u WHERE u.release_year = r.new_release) FROM releases r
    UNION ALL
    SELECT 5, 'pairs', 'confirmed_pairs', 'both', (SELECT count(*) FROM confirmed_pairs)
    UNION ALL
    SELECT 6, 'schedule', 'schedule_eligible_pairs', 'both', (SELECT count(*) FROM sched WHERE schedule_eligible)
    UNION ALL
    SELECT 7, 'schedule', 'schedule_later', 'both', (SELECT count(*) FROM sched WHERE end_date_movement_days > 0)
    UNION ALL
    SELECT 8, 'schedule', 'schedule_unchanged', 'both', (SELECT count(*) FROM sched WHERE end_date_movement_days = 0)
    UNION ALL
    SELECT 9, 'schedule', 'schedule_earlier', 'both', (SELECT count(*) FROM sched WHERE end_date_movement_days < 0)
    UNION ALL
    SELECT 10, 'schedule', 'schedule_excluded', 'both', (SELECT count(*) FROM sched WHERE NOT schedule_eligible)
    UNION ALL
    SELECT 11, 'schedule', 'schedule_excluded_end_date_unavailable_in_a_release', 'both',
           (SELECT count(*) FROM sched WHERE schedule_exclusion_reason = 'end_date_unavailable_in_a_release')
    UNION ALL
    SELECT 12, 'schedule', 'schedule_excluded_reviewer_scope_or_resequencing', 'both',
           (SELECT count(*) FROM sched WHERE schedule_exclusion_reason = 'reviewer_excluded_scope_or_resequencing')
    UNION ALL
    SELECT 13, 'dca', 'dca_eligible_pairs', 'both', (SELECT count(*) FROM dca WHERE dca_eligible)
    UNION ALL
    SELECT 14, 'dca', 'dca_unchanged', 'both', (SELECT count(*) FROM dca WHERE dca_transition = 'Unchanged')
    UNION ALL
    SELECT 15, 'dca', 'dca_improved', 'both', (SELECT count(*) FROM dca WHERE dca_transition = 'Improved')
    UNION ALL
    SELECT 16, 'dca', 'dca_deteriorated', 'both', (SELECT count(*) FROM dca WHERE dca_transition = 'Deteriorated')
    UNION ALL
    SELECT 17, 'dca', 'dca_excluded', 'both', (SELECT count(*) FROM dca WHERE NOT dca_eligible)
    UNION ALL
    SELECT 18, 'dca', 'dca_excluded_rating_unavailable_in_a_release', 'both',
           (SELECT count(*) FROM dca WHERE dca_exclusion_reason = 'rating_unavailable_in_a_release')
    UNION ALL
    SELECT 19, 'dca', 'dca_excluded_rating_field_changed', 'both',
           (SELECT count(*) FROM dca WHERE dca_exclusion_reason = 'rating_field_changed')
    UNION ALL
    SELECT 20, 'dca', 'dca_excluded_origin_unverified', 'both',
           (SELECT count(*) FROM dca WHERE dca_exclusion_reason = 'origin_unverified')
    UNION ALL
    SELECT 21, 'dca', 'dca_excluded_reviewer_scope_change', 'both',
           (SELECT count(*) FROM dca WHERE dca_exclusion_reason = 'reviewer_excluded_scope_change')
    UNION ALL
    SELECT 22, 'pairs', 'pairs_eligible_for_schedule_and_dca', 'both',
           (SELECT count(*) FROM sched s JOIN dca d USING (old_observation_id, new_observation_id)
            WHERE s.schedule_eligible AND d.dca_eligible)
    UNION ALL
    SELECT 23, 'variance', 'annual_variance_calculated', r.old_release,
           (SELECT count(*) FROM annual_variance a WHERE a.release_year = r.old_release AND a.variance_pct IS NOT NULL)
    FROM releases r
    UNION ALL
    SELECT 24, 'variance', 'annual_variance_calculated', r.new_release,
           (SELECT count(*) FROM annual_variance a WHERE a.release_year = r.new_release AND a.variance_pct IS NOT NULL)
    FROM releases r
    UNION ALL
    SELECT 25, 'variance', 'annual_variance_not_calculated', r.old_release,
           (SELECT count(*) FROM annual_variance a WHERE a.release_year = r.old_release AND a.variance_pct IS NULL)
    FROM releases r
    UNION ALL
    SELECT 26, 'variance', 'annual_variance_not_calculated', r.new_release,
           (SELECT count(*) FROM annual_variance a WHERE a.release_year = r.new_release AND a.variance_pct IS NULL)
    FROM releases r
    UNION ALL
    SELECT 27, 'variance', 'variance_within_tolerance', 'both',
           (SELECT count(*) FROM variance_reconciliation WHERE reconciliation_status = 'within_tolerance')
    UNION ALL
    SELECT 28, 'variance', 'variance_outside_tolerance_investigate', 'both',
           (SELECT count(*) FROM variance_reconciliation WHERE reconciliation_status = 'outside_tolerance_investigate')
    UNION ALL
    SELECT 29, 'variance', 'variance_not_reconciled', 'both',
           (SELECT count(*) FROM variance_reconciliation WHERE reconciliation_status LIKE 'not_compared_%')
    UNION ALL
    SELECT 30, 'money', 'monetary_comparable_pairs', 'both', (SELECT count(*) FROM confirmed_pairs WHERE monetary_comparable)
);

-- Checks -------------------------------------------------------------------------------------------

INSERT INTO sql_checks
SELECT 'R01', '04_reconciliation', 'no published variance is negative (unsigned-magnitude convention holds)',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM variance_reconciliation_all WHERE published_pct < 0);

INSERT INTO sql_checks
SELECT 'R02', '04_reconciliation', 'reconciliation keeps one row per sample observation', actual, expected,
       actual = expected
FROM (SELECT (SELECT count(*) FROM variance_reconciliation) AS actual,
             (SELECT count(*) FROM sample_observations) AS expected);

INSERT INTO sql_checks
SELECT 'R03', '04_reconciliation', 'published values equal the prepared source values (not overwritten)',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected
      FROM variance_reconciliation_all r JOIN src_observations o ON o.observation_id = r.observation_id
      WHERE r.published_raw IS DISTINCT FROM o.fy_variance_pct_raw
         OR (o.fy_variance_pct_status = 'parsed' AND r.published_pct IS DISTINCT FROM CAST(o.fy_variance_pct AS DOUBLE)));

INSERT INTO sql_checks
SELECT 'R04', '04_reconciliation', 'calculated values equal the annual variance table (not adjusted to agree)',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected
      FROM variance_reconciliation_all r JOIN annual_variance_all a ON a.observation_id = r.observation_id
      WHERE r.calculated_pct IS DISTINCT FROM a.variance_pct);

INSERT INTO sql_checks
SELECT 'R05', '04_reconciliation', 'every sample observation has one recognised reconciliation status', actual,
       expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM variance_reconciliation
      WHERE reconciliation_status IS NULL
         OR NOT (reconciliation_status IN ('within_tolerance', 'outside_tolerance_investigate',
                                          'not_compared_calculated_unavailable')
                 OR reconciliation_status LIKE 'not_compared_published_%'));

INSERT INTO sql_checks
SELECT 'R06', '04_reconciliation', 'coverage pair counts equal confirmed pair counts', actual, expected,
       actual = expected
FROM (SELECT (SELECT n FROM coverage WHERE measure = 'confirmed_pairs') AS actual,
             (SELECT count(*) FROM src_crosswalk WHERE decision = 'confirmed') AS expected);

INSERT INTO sql_checks
SELECT 'R07', '04_reconciliation',
       'coverage: eligible + excluded = confirmed pairs, outcomes sum to eligible, reasons sum to excluded',
       actual, expected, actual = expected
FROM (WITH c AS (SELECT measure, n FROM coverage WHERE release_year = 'both')
      SELECT count(*) AS actual, 0 AS expected FROM (
          SELECT (SELECT n FROM c WHERE measure = 'schedule_eligible_pairs')
               + (SELECT n FROM c WHERE measure = 'schedule_excluded') AS a,
                 (SELECT n FROM c WHERE measure = 'confirmed_pairs') AS b
          UNION ALL SELECT (SELECT sum(n) FROM c WHERE measure IN ('schedule_later', 'schedule_unchanged',
                                                                     'schedule_earlier')),
                           (SELECT n FROM c WHERE measure = 'schedule_eligible_pairs')
          UNION ALL SELECT (SELECT sum(n) FROM c WHERE measure LIKE 'schedule_excluded_%'),
                           (SELECT n FROM c WHERE measure = 'schedule_excluded')
          UNION ALL SELECT (SELECT n FROM c WHERE measure = 'dca_eligible_pairs')
                         + (SELECT n FROM c WHERE measure = 'dca_excluded'),
                           (SELECT n FROM c WHERE measure = 'confirmed_pairs')
          UNION ALL SELECT (SELECT sum(n) FROM c WHERE measure IN ('dca_unchanged', 'dca_improved', 'dca_deteriorated')),
                           (SELECT n FROM c WHERE measure = 'dca_eligible_pairs')
          UNION ALL SELECT (SELECT sum(n) FROM c WHERE measure LIKE 'dca_excluded_%'),
                           (SELECT n FROM c WHERE measure = 'dca_excluded')
      ) WHERE a IS DISTINCT FROM b);

-- Export views (Power BI) ------------------------------------------------------------------------------
-- Every export carries identifiers, source links, raw tokens, statuses and comparability flags.
-- src/build.py writes each view to data/processed/pbi_<name>.csv in a fixed order.

CREATE OR REPLACE VIEW export_observations AS
SELECT s.observation_id, s.release_year, s.snapshot_date, m.project_key,
       s.source_file, CAST(s.source_row AS INTEGER) AS source_row,
       s.source_page_url, s.download_url, s.report_url,
       s.project_id_raw, s.project_id, s.project_id_status, m.canonical_project_id, m.match_status, m.neutral_label,
       s.project_name_raw, s.department_raw, s.annual_report_category_raw,
       s.scope_include, s.scope_reason, s.scope_reviewer,
       s.start_date_raw, s.start_date, s.start_date_status, s.end_date_raw, s.end_date, s.end_date_status,
       s.external_dca_raw, s.external_dca, s.external_dca_status, s.external_dca_source_column,
       s.sro_dca_raw, s.sro_dca, s.sro_dca_status, s.sro_dca_source_column,
       g.commentary_attribution AS dca_commentary_attribution, g.origin_status AS dca_origin_status,
       g.evidence_quote AS dca_origin_evidence_quote,
       s.fy_baseline_gbp_m_raw, s.fy_baseline_gbp_m, s.fy_baseline_gbp_m_status, s.fy_baseline_gbp_m_price_basis_label,
       s.fy_forecast_gbp_m_raw, s.fy_forecast_gbp_m, s.fy_forecast_gbp_m_status, s.fy_forecast_gbp_m_price_basis_label,
       s.fy_variance_pct_raw, s.fy_variance_pct, s.fy_variance_pct_status,
       s.whole_life_cost_gbp_m_raw, s.whole_life_cost_gbp_m, s.whole_life_cost_gbp_m_status,
       s.whole_life_cost_gbp_m_price_basis_label,
       s.benefits_gbp_m_raw, s.benefits_gbp_m, s.benefits_gbp_m_status, s.benefits_gbp_m_price_basis_label,
       false AS monetary_cross_release_comparable
FROM sample_observations s
JOIN observation_match m ON m.observation_id = s.observation_id
LEFT JOIN dca_origin g ON g.observation_id = s.observation_id;

CREATE OR REPLACE VIEW export_annual_variance AS
SELECT a.observation_id, a.release_year, m.project_key, a.source_file, CAST(a.source_row AS INTEGER) AS source_row,
       s.source_page_url, s.download_url, s.project_id_raw, s.project_name_raw, a.measure_label,
       a.baseline_raw, a.baseline_status, CAST(a.baseline_gbp_m AS DOUBLE) AS baseline_gbp_m,
       a.forecast_raw, a.forecast_status, CAST(a.forecast_gbp_m AS DOUBLE) AS forecast_gbp_m,
       a.baseline_price_basis_label, a.forecast_price_basis_label, a.price_basis_consistent, a.price_basis_note,
       a.variance_status, CAST(a.variance_gbp_m AS DOUBLE) AS variance_gbp_m, a.variance_direction,
       round(a.variance_pct, 6) AS variance_pct, a.variance_pct_status, a.zero_baseline_flag, a.negative_amount_flag,
       false AS monetary_cross_release_comparable
FROM annual_variance a
JOIN sample_observations s ON s.observation_id = a.observation_id
JOIN observation_match m ON m.observation_id = a.observation_id;

CREATE OR REPLACE VIEW export_variance_reconciliation AS
SELECT r.observation_id, r.release_year, m.project_key, r.source_file, CAST(r.source_row AS INTEGER) AS source_row,
       s.source_page_url, s.download_url, s.project_id_raw, s.project_name_raw,
       round(r.calculated_pct, 6) AS calculated_pct, r.variance_pct_status,
       r.published_raw, r.published_status, r.published_pct, r.comparison_basis, r.tolerance_pp,
       round(r.difference_pp, 6) AS difference_pp, r.reconciliation_status
FROM variance_reconciliation r
JOIN sample_observations s ON s.observation_id = r.observation_id
JOIN observation_match m ON m.observation_id = r.observation_id;

CREATE OR REPLACE VIEW export_schedule_pairs AS
SELECT c.canonical_project_id AS project_key, c.canonical_project_id, c.old_observation_id, c.new_observation_id,
       o.release_year AS old_release_year, n.release_year AS new_release_year,
       CAST(o.source_row AS INTEGER) AS old_source_row, CAST(n.source_row AS INTEGER) AS new_source_row,
       o.download_url AS old_download_url, n.download_url AS new_download_url,
       o.project_id_raw AS old_project_id_raw, n.project_id_raw AS new_project_id_raw,
       o.project_name_raw AS old_project_name, n.project_name_raw AS new_project_name,
       c.match_method, c.schedule_comparable, c.dca_comparable, c.monetary_comparable,
       c.old_end_date_raw, c.old_end_date_status, c.old_end_date,
       c.new_end_date_raw, c.new_end_date_status, c.new_end_date,
       c.end_date_movement_days, c.movement_label, c.schedule_status, c.schedule_eligible,
       c.schedule_exclusion_reason,
       c.rationale, c.evidence_url, c.reviewer
FROM schedule_comparison c
JOIN sample_observations o ON o.observation_id = c.old_observation_id
JOIN sample_observations n ON n.observation_id = c.new_observation_id;

CREATE OR REPLACE VIEW export_dca_pairs AS
SELECT d.canonical_project_id AS project_key, d.canonical_project_id, d.old_observation_id, d.new_observation_id,
       o.release_year AS old_release_year, n.release_year AS new_release_year,
       CAST(o.source_row AS INTEGER) AS old_source_row, CAST(n.source_row AS INTEGER) AS new_source_row,
       o.download_url AS old_download_url, n.download_url AS new_download_url,
       o.project_id_raw AS old_project_id_raw, n.project_id_raw AS new_project_id_raw,
       o.project_name_raw AS old_project_name, n.project_name_raw AS new_project_name,
       d.match_method, d.schedule_comparable, d.dca_comparable, d.monetary_comparable,
       d.old_external_dca_raw, d.old_external_dca_status, d.old_sro_dca_raw, d.old_sro_dca_status,
       d.new_external_dca_raw, d.new_external_dca_status, d.new_sro_dca_raw, d.new_sro_dca_status,
       d.old_rating_field, d.new_rating_field, d.rating_field_changed, d.same_field,
       d.old_commentary_attribution, d.old_origin_status, d.new_commentary_attribution, d.new_origin_status,
       d.origin_verified_both,
       d.old_rating_compared, d.new_rating_compared, d.old_concern_rank, d.new_concern_rank,
       d.dca_transition, d.dca_status, d.dca_eligible, d.dca_exclusion_reason,
       d.rationale, d.evidence_url, d.reviewer
FROM dca_comparison d
JOIN sample_observations o ON o.observation_id = d.old_observation_id
JOIN sample_observations n ON n.observation_id = d.new_observation_id;

CREATE OR REPLACE VIEW export_unmatched AS
SELECT side, observation_id, release_year, project_key, other_release_year, source_file,
       CAST(source_row AS INTEGER) AS source_row,
       source_page_url, download_url, project_id_raw, project_name_raw, department_raw, annual_report_category_raw,
       scope_include, match_status, neutral_label, candidate_pairs, candidate_observation_ids
FROM unmatched_observations;

CREATE OR REPLACE VIEW export_field_availability AS
SELECT f.release_year, f.canonical_field, f.output_field, f.status, f.availability_class,
       f.observations_with_status, f.sample_observations, f.share_pct, l.file_name AS source_file, l.download_url
FROM field_availability f
JOIN src_source_links l ON l.release_year = f.release_year;

CREATE OR REPLACE VIEW export_coverage AS
SELECT sort_order, measure_group, measure, release_year, n FROM coverage;

-- Export checks: every export keeps the row count of the table it presents, and no excluded or
-- unreviewed observation appears in any export.

INSERT INTO sql_checks
SELECT 'E01', '04_reconciliation', 'export row counts equal their source tables (joins preserve rows)', actual,
       expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM (
    SELECT (SELECT count(*) FROM export_observations) AS a, (SELECT count(*) FROM sample_observations) AS b
    UNION ALL SELECT (SELECT count(*) FROM export_annual_variance), (SELECT count(*) FROM annual_variance)
    UNION ALL SELECT (SELECT count(*) FROM export_variance_reconciliation), (SELECT count(*) FROM variance_reconciliation)
    UNION ALL SELECT (SELECT count(*) FROM export_schedule_pairs), (SELECT count(*) FROM schedule_comparison)
    UNION ALL SELECT (SELECT count(*) FROM export_dca_pairs), (SELECT count(*) FROM dca_comparison)
    UNION ALL SELECT (SELECT count(*) FROM export_unmatched), (SELECT count(*) FROM unmatched_observations)
    UNION ALL SELECT (SELECT count(*) FROM export_field_availability), (SELECT count(*) FROM field_availability)
) WHERE a <> b);

INSERT INTO sql_checks
SELECT 'E02', '04_reconciliation', 'no excluded or unreviewed observation appears in any export', actual,
       expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM (
    SELECT observation_id FROM export_observations
    UNION ALL SELECT observation_id FROM export_annual_variance
    UNION ALL SELECT observation_id FROM export_variance_reconciliation
    UNION ALL SELECT old_observation_id FROM export_schedule_pairs
    UNION ALL SELECT new_observation_id FROM export_schedule_pairs
    UNION ALL SELECT old_observation_id FROM export_dca_pairs
    UNION ALL SELECT new_observation_id FROM export_dca_pairs
    UNION ALL SELECT observation_id FROM export_unmatched
) e WHERE e.observation_id NOT IN (SELECT observation_id FROM src_scope_review WHERE include = 'true'));

INSERT INTO sql_checks
SELECT 'E03', '04_reconciliation', 'observation-level exports have unique observation_id', actual, expected,
       actual = expected
FROM (SELECT (SELECT count(*) - count(DISTINCT observation_id) FROM export_observations)
           + (SELECT count(*) - count(DISTINCT observation_id) FROM export_annual_variance)
           + (SELECT count(*) - count(DISTINCT observation_id) FROM export_variance_reconciliation)
           + (SELECT count(*) - count(DISTINCT observation_id) FROM export_unmatched) AS actual, 0 AS expected);

INSERT INTO sql_checks
SELECT 'E04', '04_reconciliation', 'project_key is present and unique within each release in the observation export',
       actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM export_observations WHERE project_key IS NULL)
           + (SELECT count(*) FROM (SELECT release_year, project_key FROM export_observations
                                    GROUP BY ALL HAVING count(*) > 1)) AS actual,
             0 AS expected);

INSERT INTO sql_checks
SELECT 'E05', '04_reconciliation',
       'a project_key observed in both releases is the canonical ID of a confirmed pair, and every pair key is on both observations',
       actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM (SELECT project_key FROM export_observations GROUP BY ALL
                                    HAVING count(DISTINCT release_year) > 1) k
              WHERE k.project_key NOT IN (SELECT canonical_project_id FROM confirmed_pairs))
           + (SELECT count(*) FROM confirmed_pairs p
              WHERE (SELECT count(*) FROM export_observations o
                     WHERE o.project_key = p.canonical_project_id
                       AND o.observation_id IN (p.old_observation_id, p.new_observation_id)) <> 2) AS actual,
             0 AS expected);
