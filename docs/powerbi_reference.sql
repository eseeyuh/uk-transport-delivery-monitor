-- Reference values for validating a Power BI report built from data/processed/pbi_*.csv
-- (docs/powerbi_handoff.md section 7). Run against outputs/monitor.duckdb, read only.
-- The export_* views are the exact sources of the CSV files (check E01).
-- Each query returns (ref, context, value). Context is the filter to apply in Power BI.

SELECT 'V01' AS ref, release_year AS context, count(*) AS value
FROM export_observations GROUP BY ALL ORDER BY 2;

SELECT 'V02', 'no filter', count(DISTINCT project_key) FROM export_observations;

SELECT 'V03', r.release_year, count(u.observation_id)
FROM (SELECT DISTINCT release_year FROM export_observations) r
LEFT JOIN export_unmatched u ON u.release_year = r.release_year
GROUP BY ALL ORDER BY 2;

SELECT 'V04', 'no filter', count(*) FROM export_schedule_pairs;

SELECT 'V05', 'no filter', count(*) FILTER (WHERE schedule_eligible) FROM export_schedule_pairs;

SELECT 'V06', 'no filter', count(*) FILTER (WHERE schedule_eligible AND end_date_movement_days > 0)
FROM export_schedule_pairs;

SELECT 'V07', 'no filter', count(*) FILTER (WHERE schedule_eligible AND end_date_movement_days = 0)
FROM export_schedule_pairs;

SELECT 'V08', 'no filter', count(*) FILTER (WHERE schedule_eligible AND end_date_movement_days < 0)
FROM export_schedule_pairs;

SELECT 'V09', 'no filter', count(*) FILTER (WHERE NOT schedule_eligible) FROM export_schedule_pairs;

SELECT 'V10', schedule_exclusion_reason, count(*)
FROM export_schedule_pairs WHERE NOT schedule_eligible GROUP BY ALL ORDER BY 2;

SELECT 'V11', 'no filter', median(end_date_movement_days) FILTER (WHERE schedule_eligible)
FROM export_schedule_pairs;

SELECT 'V12', 'no filter', count(*) FILTER (WHERE dca_eligible) FROM export_dca_pairs;

SELECT 'V13', 'no filter', count(*) FILTER (WHERE dca_eligible AND dca_transition = 'Unchanged') FROM export_dca_pairs;

SELECT 'V14', 'no filter', count(*) FILTER (WHERE dca_eligible AND dca_transition = 'Improved') FROM export_dca_pairs;

SELECT 'V15', 'no filter', count(*) FILTER (WHERE dca_eligible AND dca_transition = 'Deteriorated') FROM export_dca_pairs;

SELECT 'V16', 'no filter', count(*) FILTER (WHERE NOT dca_eligible) FROM export_dca_pairs;

SELECT 'V17', dca_exclusion_reason, count(*)
FROM export_dca_pairs WHERE NOT dca_eligible GROUP BY ALL ORDER BY 2;

SELECT 'V18', release_year, count(*) FROM export_annual_variance GROUP BY ALL ORDER BY 2;

SELECT 'V19', release_year, count(*) FILTER (WHERE variance_status = 'calculated')
FROM export_annual_variance GROUP BY ALL ORDER BY 2;

SELECT 'V20', release_year, round(sum(baseline_gbp_m) FILTER (WHERE variance_status = 'calculated'), 2)
FROM export_annual_variance GROUP BY ALL ORDER BY 2;

SELECT 'V21', release_year, round(sum(forecast_gbp_m) FILTER (WHERE variance_status = 'calculated'), 2)
FROM export_annual_variance GROUP BY ALL ORDER BY 2;

SELECT 'V22', release_year,
       round(100 * (sum(forecast_gbp_m) FILTER (WHERE variance_status = 'calculated')
                    - sum(baseline_gbp_m) FILTER (WHERE variance_status = 'calculated'))
             / sum(baseline_gbp_m) FILTER (WHERE variance_status = 'calculated'), 4)
FROM export_annual_variance GROUP BY ALL ORDER BY 2;

-- V23: by design, financial measures are blank when both releases are in context.
SELECT 'V23', 'no release filter', NULL;

SELECT 'V24', release_year || ' / ' || reconciliation_status, count(*)
FROM export_variance_reconciliation GROUP BY ALL ORDER BY 2;

SELECT 'V25', release_year || ' / ' || availability_class,
       observations_with_status || ' of ' || sample_observations || ' (' || share_pct || '%)'
FROM export_field_availability WHERE output_field = 'end_date' ORDER BY 2;

SELECT 'V26', release_year,
       count(*) FILTER (WHERE dca_origin_status IN ('independent_attributed', 'sro_attributed'))
           || ' of ' || count(*) FILTER (WHERE dca_origin_status IS NOT NULL)
FROM export_observations GROUP BY ALL ORDER BY 2;

SELECT 'V27', 'no filter', count(*) FILTER (WHERE monetary_comparable) FROM export_schedule_pairs;
