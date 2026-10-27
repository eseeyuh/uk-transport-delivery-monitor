-- 01_scope.sql: observation scope, confirmed pairs and observations without a confirmed pair.
--
-- Inputs loaded by src/build.py as VARCHAR tables, unchanged from their files:
--   src_observations (data/processed/observations.csv), src_scope_review, src_crosswalk,
--   src_match_unmatched (outputs/unmatched_observations.csv), src_source_links, src_manifest,
--   src_dca_origin_review (data/manual/dca_origin_review.csv).
-- snapshot_date and retrieval_date come from the manifest (user-supplied metadata); blank means not
-- supplied and is kept as NULL, never filled from another date.
-- The sample is the reviewer's include = 'true' rows; nothing else enters analytical tables.

CREATE OR REPLACE TABLE releases AS
SELECT min(release_year) AS old_release, max(release_year) AS new_release
FROM src_observations;

CREATE OR REPLACE TABLE observation_scope AS
SELECT o.*,
       s.include AS scope_include,
       s.reason AS scope_reason,
       s.reviewer AS scope_reviewer,
       coalesce(s.include = 'true', false) AS in_sample,
       l.source_page_url,
       l.download_url,
       l.report_url,
       CAST(nullif(mf.snapshot_date, '') AS DATE) AS snapshot_date,
       CAST(nullif(mf.retrieval_date, '') AS DATE) AS retrieval_date
FROM src_observations o
LEFT JOIN src_scope_review s ON s.observation_id = o.observation_id
LEFT JOIN src_source_links l ON l.file_name = o.source_file
LEFT JOIN src_manifest mf ON mf.file_name = o.source_file;

CREATE OR REPLACE TABLE sample_observations AS
SELECT * FROM observation_scope WHERE in_sample;

-- Confirmed one-to-one pairs between sample observations. Comparability flags are the reviewer's.
CREATE OR REPLACE TABLE confirmed_pairs AS
SELECT x.canonical_project_id,
       x.old_observation_id,
       x.new_observation_id,
       x.match_method,
       x.schedule_comparable = 'true' AS schedule_comparable,
       x.dca_comparable = 'true' AS dca_comparable,
       x.monetary_comparable = 'true' AS monetary_comparable,
       x.rationale,
       x.evidence_url,
       x.reviewer
FROM src_crosswalk x
JOIN sample_observations o ON o.observation_id = x.old_observation_id
JOIN sample_observations n ON n.observation_id = x.new_observation_id
WHERE x.decision = 'confirmed';

-- Neutral wording, identical to src/match.py: absence is not completion, cancellation or a new start.
CREATE OR REPLACE TABLE match_status_labels AS
SELECT * FROM (VALUES
    ('confirmed_pair', 'observed in both releases (confirmed one-to-one pair)'),
    ('no_candidate', 'not observed in the other release (no candidate under the matching rules)'),
    ('candidates_rejected', 'not observed in the other release (all candidates rejected on review)'),
    ('pending_review', 'match not yet decided (candidates awaiting review)'),
    ('split_merge_unresolved', 'unresolved split/merge case; excluded from simple paired statistics')
) t(match_status, neutral_label);

CREATE OR REPLACE TABLE observation_match AS
WITH links AS (
    SELECT old_observation_id AS observation_id, new_observation_id AS other_observation_id, decision
    FROM src_crosswalk
    UNION ALL
    SELECT new_observation_id, old_observation_id, decision
    FROM src_crosswalk
),
status AS (
    SELECT s.observation_id,
           CASE WHEN bool_or(l.decision = 'confirmed') THEN 'confirmed_pair'
                WHEN bool_or(l.decision = 'split_merge') THEN 'split_merge_unresolved'
                WHEN bool_or(l.decision = 'needs_review') THEN 'pending_review'
                WHEN count(l.decision) > 0 THEN 'candidates_rejected'
                ELSE 'no_candidate' END AS match_status,
           count(l.decision) AS candidate_pairs,
           coalesce(string_agg(l.other_observation_id, ';' ORDER BY l.other_observation_id), '')
               AS candidate_observation_ids
    FROM sample_observations s
    LEFT JOIN links l ON l.observation_id = s.observation_id
    GROUP BY s.observation_id
),
canonical AS (
    SELECT old_observation_id AS observation_id, canonical_project_id FROM confirmed_pairs
    UNION ALL
    SELECT new_observation_id, canonical_project_id FROM confirmed_pairs
)
-- project_key identifies a project across releases for reporting: the canonical ID of a confirmed pair,
-- otherwise the observation's own parsed project ID (NULL if none; checked in 04_reconciliation.sql).
SELECT st.*, lb.neutral_label, c.canonical_project_id,
       coalesce(c.canonical_project_id, nullif(so.project_id, '')) AS project_key
FROM status st
JOIN match_status_labels lb ON lb.match_status = st.match_status
JOIN sample_observations so ON so.observation_id = st.observation_id
LEFT JOIN canonical c ON c.observation_id = st.observation_id;

CREATE OR REPLACE TABLE unmatched_observations AS
SELECT CASE WHEN s.release_year = r.old_release THEN 'old' ELSE 'new' END AS side,
       s.observation_id,
       s.release_year,
       CASE WHEN s.release_year = r.old_release THEN r.new_release ELSE r.old_release END AS other_release_year,
       s.source_file,
       s.source_row,
       s.source_page_url,
       s.download_url,
       m.project_key,
       s.project_id_raw,
       s.project_name_raw,
       s.department_raw,
       s.annual_report_category_raw,
       s.scope_include,
       m.match_status,
       m.neutral_label,
       m.candidate_pairs,
       m.candidate_observation_ids
FROM sample_observations s
JOIN observation_match m ON m.observation_id = s.observation_id
CROSS JOIN releases r
WHERE m.match_status <> 'confirmed_pair';

-- Checks -------------------------------------------------------------------------------------------

INSERT INTO sql_checks
SELECT 'S01', '01_scope', 'observation_id is unique in observations', actual, expected, actual = expected
FROM (SELECT count(*) - count(DISTINCT observation_id) AS actual, 0 AS expected FROM src_observations);

INSERT INTO sql_checks
SELECT 'S02', '01_scope', 'scope and source-link joins keep one row per observation', actual, expected,
       actual = expected
FROM (SELECT (SELECT count(*) FROM observation_scope) AS actual, (SELECT count(*) FROM src_observations) AS expected);

INSERT INTO sql_checks
SELECT 'S03', '01_scope', 'every scope review row joins to one observation', actual, expected, actual = expected
FROM (SELECT count(*) AS actual, (SELECT count(*) FROM src_scope_review) AS expected
      FROM src_scope_review s JOIN src_observations o ON o.observation_id = s.observation_id);

INSERT INTO sql_checks
SELECT 'S04', '01_scope', 'sample rows equal scope review rows with include = true', actual, expected,
       actual = expected
FROM (SELECT (SELECT count(*) FROM sample_observations) AS actual,
             (SELECT count(*) FROM src_scope_review WHERE include = 'true') AS expected);

INSERT INTO sql_checks
SELECT 'S05', '01_scope', 'sample contains no excluded or unreviewed observation', actual, expected,
       actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM sample_observations WHERE scope_include IS DISTINCT FROM 'true');

INSERT INTO sql_checks
SELECT 'S06', '01_scope', 'confirmed pairs joined to sample observations equal confirmed crosswalk rows',
       actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM confirmed_pairs) AS actual,
             (SELECT count(*) FROM src_crosswalk WHERE decision = 'confirmed') AS expected);

INSERT INTO sql_checks
SELECT 'S07', '01_scope', 'confirmed pairs repeat no old or new observation (no join multiplication)',
       actual, expected, actual = expected
FROM (SELECT (count(*) - count(DISTINCT old_observation_id)) + (count(*) - count(DISTINCT new_observation_id))
             AS actual, 0 AS expected FROM confirmed_pairs);

INSERT INTO sql_checks
SELECT 'S08', '01_scope', 'every sample observation has exactly one match status row', actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM observation_match) AS actual, (SELECT count(*) FROM sample_observations) AS expected);

INSERT INTO sql_checks
SELECT 'S09', '01_scope', 'sample observations per side = confirmed pairs + observations without a confirmed pair',
       actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM confirmed_pairs) * 2 + (SELECT count(*) FROM unmatched_observations) AS actual,
             (SELECT count(*) FROM sample_observations) AS expected);

INSERT INTO sql_checks
SELECT 'S10', '01_scope', 'unmatched statuses and labels equal outputs/unmatched_observations.csv from src/match.py',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM (
    (SELECT observation_id, match_status, neutral_label FROM unmatched_observations
     EXCEPT SELECT observation_id, match_status, neutral_label FROM src_match_unmatched)
    UNION ALL
    (SELECT observation_id, match_status, neutral_label FROM src_match_unmatched
     EXCEPT SELECT observation_id, match_status, neutral_label FROM unmatched_observations)));

INSERT INTO sql_checks
SELECT 'S11', '01_scope', 'no unmatched observation is labelled completed, cancelled, closed, exited or new',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM unmatched_observations
      WHERE regexp_matches(lower(match_status || ' ' || neutral_label), 'complet|cancel|closed|exit|new project|new start'));

INSERT INTO sql_checks
SELECT 'S12', '01_scope', 'every source file has one verified link whose SHA256 equals the manifest',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, (SELECT count(DISTINCT source_file) FROM src_observations) AS expected
      FROM src_source_links l JOIN src_manifest m ON m.file_name = l.file_name AND m.sha256 = l.sha256
      WHERE l.file_name IN (SELECT source_file FROM src_observations));

INSERT INTO sql_checks
SELECT 'S13', '01_scope', 'manifest release_year, where supplied, equals the release_year of every observation in that file',
       actual, expected, actual = expected
FROM (SELECT count(*) AS actual, 0 AS expected FROM src_observations o
      JOIN src_manifest m ON m.file_name = o.source_file
      WHERE m.release_year <> '' AND m.release_year <> o.release_year);

INSERT INTO sql_checks
SELECT 'S14', '01_scope', 'every release has at most one snapshot date, and supplied dates are valid',
       actual, expected, actual = expected
FROM (SELECT (SELECT count(*) FROM src_manifest
              WHERE (snapshot_date <> '' AND TRY_CAST(snapshot_date AS DATE) IS NULL)
                 OR (retrieval_date <> '' AND TRY_CAST(retrieval_date AS DATE) IS NULL))
           + (SELECT count(*) FROM (SELECT release_year FROM observation_scope GROUP BY release_year
                                    HAVING count(DISTINCT snapshot_date) > 1)) AS actual,
             0 AS expected);
