-- 03_comparison.sql: within-release annual forecast variance, end-date movement and DCA transitions.
--
-- Annual forecast variance against annual baseline (methodology section 4.1):
--   computed only when baseline and forecast are both parsed single values in the same observation
--   (so the same release), with the same unit (both GBP m, enforced by the approved mapping) and the
--   same price-basis label. Amounts are exact DECIMAL(38,6); the percentage needs baseline > 0. A
--   zero baseline is kept as a value but gives a null percentage and zero_baseline_flag, never infinity.
--   No amount is compared across releases.
-- End-date movement (section 6): new minus old published end date in days, only for confirmed pairs
--   that the reviewer marked schedule_comparable and whose two end dates are parsed dates.
-- DCA transitions (section 7): only for pairs marked dca_comparable, using ratings from the same field
--   in both releases, on the verified three-category scale GREEN < AMBER < RED (order of concern).
--   No mapping exists for historical mixed ratings (e.g. Amber/Red); any rating outside the scale is
--   excluded with a reason. A missing, withheld or excluded rating never produces a transition.
-- Assessment origin (section 7): the field label alone does not establish who assessed. The reviewed
--   attribution in src_dca_origin_review (who the commentary credits with the rating, with a verbatim
--   quote checked against the commentary) must agree with the field: IPA or NISTA for the external
--   field, the SRO for the SRO field. IPA (2024-25) and NISTA (2025-26) are both independent.
-- Every confirmed pair without a calculated movement or transition carries one exclusion reason, so a
--   missing or ineligible comparison is never counted as unchanged.

CREATE OR REPLACE TABLE annual_variance_all AS
WITH amounts AS (
    SELECT observation_id,
           release_year,
           source_file,
           source_row,
           in_sample,
           fy_baseline_gbp_m_raw AS baseline_raw,
           fy_baseline_gbp_m_status AS baseline_status,
           CASE WHEN fy_baseline_gbp_m_status = 'parsed' THEN CAST(fy_baseline_gbp_m AS DECIMAL(38, 6)) END
               AS baseline_gbp_m,
           fy_baseline_gbp_m_price_basis_label AS baseline_price_basis_label,
           fy_forecast_gbp_m_raw AS forecast_raw,
           fy_forecast_gbp_m_status AS forecast_status,
           CASE WHEN fy_forecast_gbp_m_status = 'parsed' THEN CAST(fy_forecast_gbp_m AS DECIMAL(38, 6)) END
               AS forecast_gbp_m,
           fy_forecast_gbp_m_price_basis_label AS forecast_price_basis_label,
           fy_baseline_gbp_m_price_basis_label = fy_forecast_gbp_m_price_basis_label AS price_basis_consistent
    FROM observation_scope
),
variance AS (
    SELECT *,
           CASE WHEN baseline_status <> 'parsed' THEN 'baseline_' || baseline_status
                WHEN forecast_status <> 'parsed' THEN 'forecast_' || forecast_status
                WHEN NOT price_basis_consistent THEN 'price_basis_labels_differ'
                ELSE 'calculated' END AS variance_status,
           CASE WHEN baseline_status = 'parsed' AND forecast_status = 'parsed' AND price_basis_consistent
                THEN forecast_gbp_m - baseline_gbp_m END AS variance_gbp_m
    FROM amounts
)
SELECT *,
       'annual forecast variance against annual baseline' AS measure_label,
       CASE WHEN baseline_price_basis_label = 'not_stated_in_label' THEN 'same label; price basis not stated'
            WHEN price_basis_consistent THEN 'same stated basis: ' || baseline_price_basis_label
            ELSE 'labels differ' END AS price_basis_note,
       CASE WHEN variance_gbp_m IS NOT NULL AND baseline_gbp_m > 0
            THEN CAST(variance_gbp_m AS DOUBLE) / CAST(baseline_gbp_m AS DOUBLE) * 100 END AS variance_pct,
       CASE WHEN variance_gbp_m IS NULL THEN 'variance_not_calculated'
            WHEN baseline_gbp_m = 0 THEN 'zero_baseline_no_percentage'
            WHEN baseline_gbp_m < 0 THEN 'negative_baseline_review'
            ELSE 'calculated' END AS variance_pct_status,
       coalesce(variance_gbp_m IS NOT NULL AND baseline_gbp_m = 0, false) AS zero_baseline_flag,
       coalesce(baseline_gbp_m < 0, false) OR coalesce(forecast_gbp_m < 0, false) AS negative_amount_flag,
       CASE WHEN variance_gbp_m > 0 THEN 'forecast_above_baseline'
            WHEN variance_gbp_m < 0 THEN 'forecast_below_baseline'
            WHEN variance_gbp_m = 0 THEN 'forecast_equals_baseline' END AS variance_direction
FROM variance;

CREATE OR REPLACE TABLE annual_variance AS
SELECT * FROM annual_variance_all WHERE in_sample;

CREATE OR REPLACE TABLE schedule_comparison AS
WITH dates AS (
    SELECT p.*,
           o.end_date_raw AS old_end_date_raw,
           o.end_date_status AS old_end_date_status,
           CASE WHEN o.end_date_status = 'parsed' THEN CAST(o.end_date AS DATE) END AS old_end_date,
           n.end_date_raw AS new_end_date_raw,
           n.end_date_status AS new_end_date_status,
           CASE WHEN n.end_date_status = 'parsed' THEN CAST(n.end_date AS DATE) END AS new_end_date
    FROM confirmed_pairs p
    JOIN sample_observations o ON o.observation_id = p.old_observation_id
    JOIN sample_observations n ON n.observation_id = p.new_observation_id
)
, movement AS (
    SELECT *,
           CASE WHEN schedule_comparable AND old_end_date IS NOT NULL AND new_end_date IS NOT NULL
                THEN date_diff('day', old_end_date, new_end_date) END AS end_date_movement_days,
           CASE WHEN NOT schedule_comparable THEN 'not_schedule_comparable'
                WHEN old_end_date IS NULL OR new_end_date IS NULL THEN 'end_date_unavailable'
                ELSE 'calculated' END AS schedule_status
    FROM dates
)
SELECT *,
       CASE WHEN end_date_movement_days > 0 THEN 'later published end date'
            WHEN end_date_movement_days < 0 THEN 'earlier published end date'
            WHEN end_date_movement_days = 0 THEN 'unchanged published end date' END AS movement_label,
       schedule_status = 'calculated' AS schedule_eligible,
       CASE WHEN schedule_status = 'calculated' THEN NULL
            WHEN old_end_date IS NULL OR new_end_date IS NULL THEN 'end_date_unavailable_in_a_release'
            ELSE 'reviewer_excluded_scope_or_resequencing' END AS schedule_exclusion_reason
FROM movement;

CREATE OR REPLACE TABLE dca_scale AS
SELECT * FROM (VALUES ('GREEN', 1), ('AMBER', 2), ('RED', 3)) t(rating, concern_rank);

CREATE OR REPLACE TABLE dca_origin AS
WITH classified AS (
    SELECT r.*,
           CASE WHEN r.commentary_attribution IN ('IPA', 'NISTA') AND r.rating_field = 'external'
                    THEN 'independent_attributed'
                WHEN r.commentary_attribution = 'SRO' AND r.rating_field = 'sro' THEN 'sro_attributed'
                WHEN r.commentary_attribution = 'NISTA/SRO' THEN 'joint_nista_sro_attribution'
                WHEN r.commentary_attribution = 'none' THEN 'assessor_not_stated'
                ELSE 'field_and_commentary_disagree' END AS origin_status
    FROM src_dca_origin_review r
)
SELECT *,
       origin_status IN ('independent_attributed', 'sro_attributed') AS origin_verified,
       CASE origin_status WHEN 'independent_attributed' THEN 'independent'
                          WHEN 'sro_attributed' THEN 'sro' END AS origin_class
FROM classified;

CREATE OR REPLACE TABLE dca_comparison AS
WITH ratings AS (
    SELECT p.*,
           o.external_dca_raw AS old_external_dca_raw, o.external_dca_status AS old_external_dca_status,
           o.sro_dca_raw AS old_sro_dca_raw, o.sro_dca_status AS old_sro_dca_status,
           n.external_dca_raw AS new_external_dca_raw, n.external_dca_status AS new_external_dca_status,
           n.sro_dca_raw AS new_sro_dca_raw, n.sro_dca_status AS new_sro_dca_status,
           CASE WHEN o.external_dca_status = 'parsed' THEN 'external'
                WHEN o.sro_dca_status = 'parsed' THEN 'sro' END AS old_rating_field,
           CASE WHEN n.external_dca_status = 'parsed' THEN 'external'
                WHEN n.sro_dca_status = 'parsed' THEN 'sro' END AS new_rating_field,
           CASE WHEN o.external_dca_status = 'parsed' AND n.external_dca_status = 'parsed' THEN 'external'
                WHEN o.sro_dca_status = 'parsed' AND n.sro_dca_status = 'parsed' THEN 'sro' END AS same_field,
           CASE WHEN o.external_dca_status = 'parsed' AND n.external_dca_status = 'parsed'
                THEN o.external_dca ELSE o.sro_dca END AS old_same_field_rating,
           CASE WHEN o.external_dca_status = 'parsed' AND n.external_dca_status = 'parsed'
                THEN n.external_dca ELSE n.sro_dca END AS new_same_field_rating
    FROM confirmed_pairs p
    JOIN sample_observations o ON o.observation_id = p.old_observation_id
    JOIN sample_observations n ON n.observation_id = p.new_observation_id
),
scored AS (
    SELECT r.*,
           CASE WHEN r.dca_comparable AND r.same_field IS NOT NULL THEN r.old_same_field_rating END
               AS old_rating_compared,
           CASE WHEN r.dca_comparable AND r.same_field IS NOT NULL THEN r.new_same_field_rating END
               AS new_rating_compared
    FROM ratings r
)
, transitions AS (
    SELECT s.* EXCLUDE (old_same_field_rating, new_same_field_rating),
           so.concern_rank AS old_concern_rank,
           sn.concern_rank AS new_concern_rank,
           coalesce(s.old_rating_field <> s.new_rating_field, false) AS rating_field_changed,
           og_old.commentary_attribution AS old_commentary_attribution,
           og_old.origin_status AS old_origin_status,
           og_new.commentary_attribution AS new_commentary_attribution,
           og_new.origin_status AS new_origin_status,
           coalesce(og_old.origin_verified AND og_new.origin_verified
                    AND og_old.origin_class = og_new.origin_class, false) AS origin_verified_both,
           CASE WHEN so.concern_rank IS NOT NULL AND sn.concern_rank IS NOT NULL THEN
                CASE WHEN sn.concern_rank < so.concern_rank THEN 'Improved'
                     WHEN sn.concern_rank = so.concern_rank THEN 'Unchanged'
                     ELSE 'Deteriorated' END END AS dca_transition,
           CASE WHEN NOT s.dca_comparable THEN 'not_dca_comparable'
                WHEN s.same_field IS NULL THEN 'rating_unavailable_in_same_field'
                WHEN so.concern_rank IS NULL OR sn.concern_rank IS NULL THEN 'rating_outside_three_category_scale'
                ELSE 'compared' END AS dca_status
    FROM scored s
    LEFT JOIN dca_scale so ON so.rating = s.old_rating_compared
    LEFT JOIN dca_scale sn ON sn.rating = s.new_rating_compared
    LEFT JOIN dca_origin og_old ON og_old.observation_id = s.old_observation_id
    LEFT JOIN dca_origin og_new ON og_new.observation_id = s.new_observation_id
)
SELECT *,
       dca_status = 'compared' AS dca_eligible,
       CASE WHEN dca_status = 'compared' THEN NULL
            WHEN old_rating_field IS NULL OR new_rating_field IS NULL THEN 'rating_unavailable_in_a_release'
            WHEN rating_field_changed THEN 'rating_field_changed'
            WHEN NOT origin_verified_both THEN 'origin_unverified'
            ELSE 'reviewer_excluded_scope_change' END AS dca_exclusion_reason
FROM transitions;

-- Checks -------------------------------------------------------------------------------------------

INSERT INTO sql_checks
SELECT 'C01', '03_comparison', 'annual variance keeps one row per observation', actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM annual_variance_all) AS actual, (SELECT count(*) FROM src_observations) AS expected);

INSERT INTO sql_checks
SELECT 'C02', '03_comparison', 'variance only where baseline and forecast are parsed with the same price-basis label',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM annual_variance_all
      WHERE variance_gbp_m IS NOT NULL
        AND NOT (baseline_status = 'parsed' AND forecast_status = 'parsed' AND price_basis_consistent));

INSERT INTO sql_checks
SELECT 'C03', '03_comparison', 'variance percentage is never infinite or NaN', actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM annual_variance_all
      WHERE variance_pct IS NOT NULL AND (isinf(variance_pct) OR isnan(variance_pct)));

INSERT INTO sql_checks
SELECT 'C04', '03_comparison', 'variance percentage is null whenever baseline is not above zero', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM annual_variance_all
      WHERE variance_pct IS NOT NULL AND NOT baseline_gbp_m > 0);

INSERT INTO sql_checks
SELECT 'C05', '03_comparison', 'every calculated variance with a zero baseline is flagged', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM annual_variance_all
      WHERE variance_gbp_m IS NOT NULL AND baseline_gbp_m = 0
        AND NOT (zero_baseline_flag AND variance_pct_status = 'zero_baseline_no_percentage'));

INSERT INTO sql_checks
SELECT 'C06', '03_comparison', 'schedule and DCA comparisons keep one row per confirmed pair', actual, expected,
       actual = expected
FROM (SELECT (SELECT count(*) FROM schedule_comparison) + (SELECT count(*) FROM dca_comparison) AS actual,
             2 * (SELECT count(*) FROM confirmed_pairs) AS expected);

INSERT INTO sql_checks
SELECT 'C07', '03_comparison', 'end-date movement only for schedule-comparable pairs', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM schedule_comparison
      WHERE end_date_movement_days IS NOT NULL AND NOT schedule_comparable);

INSERT INTO sql_checks
SELECT 'C08', '03_comparison', 'every schedule-comparable pair has two valid end dates and a movement',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM schedule_comparison
      WHERE schedule_comparable AND end_date_movement_days IS NULL);

INSERT INTO sql_checks
SELECT 'C09', '03_comparison', 'DCA transitions only for DCA-comparable pairs with same-field ratings on the scale',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM dca_comparison
      WHERE dca_transition IS NOT NULL
        AND NOT (dca_comparable AND same_field IS NOT NULL AND old_concern_rank IS NOT NULL
                 AND new_concern_rank IS NOT NULL));

INSERT INTO sql_checks
SELECT 'C10', '03_comparison', 'a missing, withheld or blank rating never yields a transition', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM dca_comparison
      WHERE dca_transition IS NOT NULL
        AND NOT coalesce(
            (same_field = 'external' AND old_external_dca_status = 'parsed' AND new_external_dca_status = 'parsed')
            OR (same_field = 'sro' AND old_sro_dca_status = 'parsed' AND new_sro_dca_status = 'parsed'), false));

INSERT INTO sql_checks
SELECT 'C11', '03_comparison', 'every DCA-comparable pair has a transition', actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM dca_comparison WHERE dca_comparable AND dca_transition IS NULL);

INSERT INTO sql_checks
SELECT 'C12', '03_comparison', 'no observation without a confirmed pair enters a schedule or DCA comparison',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM unmatched_observations u
      WHERE u.observation_id IN (SELECT old_observation_id FROM schedule_comparison
                                 UNION SELECT new_observation_id FROM schedule_comparison
                                 UNION SELECT old_observation_id FROM dca_comparison
                                 UNION SELECT new_observation_id FROM dca_comparison));

INSERT INTO sql_checks
SELECT 'C13', '03_comparison', 'no pair is flagged monetary-comparable (cross-release money disabled)', actual,
       expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM src_crosswalk WHERE monetary_comparable <> 'false');

INSERT INTO sql_checks
SELECT 'C14', '03_comparison',
       'every sample observation with a parsed DCA rating has one origin review row with the same field and rating',
       actual, expected, actual = expected
FROM (WITH rated AS (
          SELECT observation_id,
                 CASE WHEN external_dca_status = 'parsed' THEN 'external' ELSE 'sro' END AS rating_field,
                 CASE WHEN external_dca_status = 'parsed' THEN external_dca ELSE sro_dca END AS rating
          FROM sample_observations WHERE external_dca_status = 'parsed' OR sro_dca_status = 'parsed')
      SELECT count(*) AS actual, 0 AS expected FROM (
          (SELECT observation_id, rating_field, rating FROM rated
           EXCEPT SELECT observation_id, rating_field, rating FROM src_dca_origin_review)
          UNION ALL
          (SELECT observation_id, rating_field, rating FROM src_dca_origin_review
           EXCEPT SELECT observation_id, rating_field, rating FROM rated)));

INSERT INTO sql_checks
SELECT 'C15', '03_comparison',
       'every origin evidence quote occurs verbatim in the commentary; only an unattributed rating may lack one',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected
      FROM src_dca_origin_review r JOIN src_observations o ON o.observation_id = r.observation_id
      WHERE (r.evidence_quote <> '' AND NOT contains(o.delivery_confidence_commentary_raw, r.evidence_quote))
         OR (r.evidence_quote = '' AND r.commentary_attribution <> 'none'));

INSERT INTO sql_checks
SELECT 'C16', '03_comparison', 'every DCA-comparable pair has verified origin of the same class in both releases',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM dca_comparison WHERE dca_comparable AND NOT origin_verified_both);

INSERT INTO sql_checks
SELECT 'C17', '03_comparison', 'every pair has either a calculated comparison or exactly one exclusion reason',
       actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM schedule_comparison
              WHERE (end_date_movement_days IS NULL) = (schedule_exclusion_reason IS NULL)
                 OR schedule_eligible <> (end_date_movement_days IS NOT NULL))
           + (SELECT count(*) FROM dca_comparison
              WHERE (dca_transition IS NULL) = (dca_exclusion_reason IS NULL)
                 OR dca_eligible <> (dca_transition IS NOT NULL)) AS actual,
             0 AS expected);

INSERT INTO sql_checks
SELECT 'C18', '03_comparison',
       'every pair excluded by the reviewer on scope cites scope or re-sequencing in its rationale',
       actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM schedule_comparison
              WHERE schedule_exclusion_reason = 'reviewer_excluded_scope_or_resequencing'
                AND NOT regexp_matches(lower(rationale), 'scope|sequenc'))
           + (SELECT count(*) FROM dca_comparison
              WHERE dca_exclusion_reason = 'reviewer_excluded_scope_change'
                AND NOT regexp_matches(lower(rationale), 'scope')) AS actual,
             0 AS expected);
