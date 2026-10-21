-- =====================================================================================
-- FMAPI Capacity, Latency & Errors Dashboard - Data Model
--
-- Source: system.ai_gateway.usage (one row per request through Unity AI Gateway / FMAPI;
--         metadata only - no prompts or responses, so it works with Zero Data Retention)
--         system.billing.usage + system.billing.list_prices (spend)
--         system.access.workspaces_latest (workspace names)
--
-- Run once in a SQL editor on a serverless SQL warehouse. Materialized views refresh hourly.
-- To use a different catalog/schema, find/replace `main.fmapi_observability` here and in
-- the .lvdash.json.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS main.fmapi_observability;


-- -------------------------------------------------------------------------------------
-- 1. Capacity limits (maintained by hand)
--    Workspace-level FMAPI limits (ITPM / OTPM) are not exposed in system tables, so they
--    live here. Add a new row whenever a limit changes; the dashboard uses the latest
--    effective_from per workspace + model.
--    model_key = lower-case model name with spaces/dots replaced by dashes,
--    e.g. 'claude-opus-5-5', 'claude-fable-5-1', 'gpt-6-1-sol', 'gemini-3-5-flash-lite'.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS main.fmapi_observability.capacity_limits (
  workspace_id    STRING  COMMENT 'Workspace ID',
  workspace_name  STRING  COMMENT 'Friendly name shown on the dashboard (optional)',
  model_key       STRING  COMMENT 'Normalized model name, e.g. claude-opus-5-5',
  itpm_limit      BIGINT  COMMENT 'Input tokens per minute limit (includes cached input)',
  otpm_limit      BIGINT  COMMENT 'Output tokens per minute limit (NULL if none)',
  effective_from  DATE    COMMENT 'Date the limit took effect',
  source          STRING  COMMENT 'Where the number came from, e.g. capacity ticket ID',
  notes           STRING
)
COMMENT 'FMAPI capacity allocations per workspace and model. Maintained manually.';

-- Example rows - replace with your confirmed allocations before running:
-- INSERT INTO main.fmapi_observability.capacity_limits VALUES
--   ('<workspace_id>', 'Production', 'claude-sonnet-5-5', 10000000, 2000000, DATE'2026-10-01', '<ticket>', ''),
--   ('<workspace_id>', 'Production', 'gpt-6-1-sol',        5000000, 1000000, DATE'2026-10-01', '<ticket>', ''),
--   ('<workspace_id>', 'Dev',        'claude-opus-5-5',    2000000,    NULL, DATE'2026-10-01', '<ticket>', '');


-- -------------------------------------------------------------------------------------
-- 2. Workspace names
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE MATERIALIZED VIEW main.fmapi_observability.workspaces
SCHEDULE EVERY 1 HOUR
AS
SELECT CAST(workspace_id AS STRING) AS workspace_id, workspace_name
FROM system.access.workspaces_latest;


-- -------------------------------------------------------------------------------------
-- 3. Request-level fact table (one row per model request)
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE MATERIALIZED VIEW main.fmapi_observability.requests
CLUSTER BY (event_date, workspace_id, model_key)
SCHEDULE EVERY 1 HOUR
AS
SELECT
  u.account_id,
  u.workspace_id,
  u.request_id,
  u.event_time,
  DATE_TRUNC('MINUTE', u.event_time)                                AS event_minute,
  DATE_TRUNC('HOUR', u.event_time)                                  AS event_hour,
  CAST(u.event_time AS DATE)                                        AS event_date,
  u.endpoint_name,
  u.destination_type,
  u.destination_name,
  u.destination_model,
  -- requests rejected before routing (e.g. 429s) have no destination_model,
  -- so fall back to the configured route, then the endpoint name
  COALESCE(
    regexp_replace(lower(u.destination_model), '[ .]', '-'),
    regexp_replace(element_at(u.model_metadata.config.routing.destinations, 1).name, '^system\\.ai\\.(databricks-)?', ''),
    CASE WHEN u.endpoint_name LIKE 'system.ai.%'
         THEN regexp_replace(u.endpoint_name, '^system\\.ai\\.(databricks-)?', '') END,
    u.endpoint_name)                                                AS model_key,
  CASE
    WHEN model_key RLIKE 'claude'           THEN 'Anthropic'
    WHEN model_key RLIKE 'gpt-oss'          THEN 'Open source'
    WHEN model_key RLIKE '^(gpt|o[0-9])'    THEN 'OpenAI'
    WHEN model_key RLIKE 'gemini'           THEN 'Google'
    ELSE 'Open source'
  END                                                               AS model_provider,
  u.api_type,
  u.requester,
  u.requester_type,
  u.status_code,
  CASE
    WHEN u.status_code BETWEEN 200 AND 299 THEN 'Success (2xx)'
    WHEN u.status_code = 429               THEN 'Rate limited (429)'
    WHEN u.status_code BETWEEN 400 AND 499 THEN 'Client error (4xx)'
    WHEN u.status_code >= 500              THEN 'Server error (5xx)'
    ELSE 'Other'
  END                                                               AS status_class,
  u.latency_ms,
  u.time_to_first_byte_ms                                           AS ttfb_ms,
  COALESCE(u.response_content_type, '') LIKE '%event-stream%'       AS is_streaming,
  COALESCE(u.input_tokens, 0)                                       AS input_tokens,   -- includes cached input
  COALESCE(u.token_details.cache_read_input_tokens, 0)              AS cache_read_tokens,
  COALESCE(u.token_details.cache_creation_input_tokens, 0)          AS cache_write_tokens,
  COALESCE(u.output_tokens, 0)                                      AS output_tokens,
  COALESCE(u.token_details.output_reasoning_tokens, 0)              AS reasoning_tokens,
  COALESCE(size(u.routing_information.attempts), 0) > 1             AS had_fallback,
  u.invocation_metadata.service_tier                                AS service_tier,
  u.request_tags
FROM system.ai_gateway.usage u
WHERE u.mcp_metadata IS NULL;                   -- model traffic only (excludes MCP)


-- -------------------------------------------------------------------------------------
-- 4. Per-minute metrics (basis for TPM / RPM peaks vs limits)
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE MATERIALIZED VIEW main.fmapi_observability.minute_metrics
CLUSTER BY (event_minute, workspace_id, model_key)
SCHEDULE EVERY 1 HOUR
AS
SELECT
  workspace_id,
  model_key,
  model_provider,
  event_minute,
  DATE_TRUNC('HOUR', event_minute)                                  AS event_hour,
  COUNT(*)                                                          AS requests,
  COUNT_IF(status_code BETWEEN 200 AND 299)                         AS success_requests,
  COUNT_IF(status_code = 429)                                       AS errors_429,
  COUNT_IF(status_code >= 500)                                      AS errors_5xx,
  SUM(input_tokens)                                                 AS itpm,
  SUM(input_tokens - cache_read_tokens - cache_write_tokens)        AS uncached_itpm,
  SUM(cache_read_tokens)                                            AS cache_read_tpm,
  SUM(cache_write_tokens)                                           AS cache_write_tpm,
  SUM(output_tokens)                                                AS otpm,
  SUM(input_tokens + output_tokens)                                 AS total_tpm
FROM main.fmapi_observability.requests
GROUP BY ALL;


-- -------------------------------------------------------------------------------------
-- 5. Hourly metrics (peaks, errors, latency percentiles) - powers the time-series page
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE MATERIALIZED VIEW main.fmapi_observability.hourly_metrics
CLUSTER BY (event_hour, workspace_id, model_key)
SCHEDULE EVERY 1 HOUR
AS
WITH req AS (
  SELECT
    workspace_id,
    model_key,
    event_hour,
    COUNT(*)                                                        AS requests,
    COUNT_IF(status_code BETWEEN 200 AND 299)                       AS success_requests,
    COUNT_IF(status_code = 429)                                     AS errors_429,
    COUNT_IF(status_code BETWEEN 400 AND 499 AND status_code <> 429) AS errors_4xx,
    COUNT_IF(status_code >= 500)                                    AS errors_5xx,
    SUM(input_tokens)                                               AS input_tokens,
    SUM(cache_read_tokens)                                          AS cache_read_tokens,
    SUM(output_tokens)                                              AS output_tokens,
    PERCENTILE_APPROX(latency_ms, 0.50)                             AS latency_p50_ms,
    PERCENTILE_APPROX(latency_ms, 0.95)                             AS latency_p95_ms,
    PERCENTILE_APPROX(latency_ms, 0.99)                             AS latency_p99_ms,
    PERCENTILE_APPROX(ttfb_ms, 0.50)                                AS ttfb_p50_ms,
    PERCENTILE_APPROX(ttfb_ms, 0.95)                                AS ttfb_p95_ms,
    PERCENTILE_APPROX(ttfb_ms, 0.99)                                AS ttfb_p99_ms
  FROM main.fmapi_observability.requests
  GROUP BY ALL
),
mins AS (
  SELECT
    workspace_id,
    model_key,
    event_hour,
    ANY_VALUE(model_provider)                                       AS model_provider,
    MAX(itpm)                                                       AS peak_itpm,
    MAX(uncached_itpm)                                              AS peak_uncached_itpm,
    MAX(cache_read_tpm)                                             AS peak_cache_read_tpm,
    MAX(cache_write_tpm)                                            AS peak_cache_write_tpm,
    MAX(otpm)                                                       AS peak_otpm,
    MAX(total_tpm)                                                  AS peak_total_tpm,
    MAX(requests)                                                   AS peak_rpm
  FROM main.fmapi_observability.minute_metrics
  GROUP BY ALL
)
SELECT
  m.workspace_id,
  m.model_key,
  m.model_provider,
  m.event_hour,
  CAST(m.event_hour AS DATE)                                        AS event_date,
  m.peak_itpm, m.peak_uncached_itpm, m.peak_cache_read_tpm, m.peak_cache_write_tpm,
  m.peak_otpm, m.peak_total_tpm, m.peak_rpm,
  r.requests, r.success_requests, r.errors_429, r.errors_4xx, r.errors_5xx,
  r.input_tokens, r.cache_read_tokens, r.output_tokens,
  r.latency_p50_ms, r.latency_p95_ms, r.latency_p99_ms,
  r.ttfb_p50_ms, r.ttfb_p95_ms, r.ttfb_p99_ms
FROM mins m
JOIN req r USING (workspace_id, model_key, event_hour);


-- -------------------------------------------------------------------------------------
-- 6. Daily spend per model (list price)
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE MATERIALIZED VIEW main.fmapi_observability.daily_model_spend
CLUSTER BY (usage_date, workspace_id)
SCHEDULE EVERY 1 HOUR
AS
SELECT
  u.usage_date,
  CAST(u.workspace_id AS STRING)                                    AS workspace_id,
  u.usage_metadata.endpoint_name                                    AS endpoint_name,
  regexp_replace(u.usage_metadata.endpoint_name, '^databricks-', '') AS model_key,
  u.sku_name,
  SUM(u.usage_quantity)                                             AS dbus,
  SUM(u.usage_quantity * COALESCE(lp.pricing.effective_list.default, lp.pricing.default)) AS list_cost_usd
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices lp
  ON  u.sku_name   = lp.sku_name
  AND u.cloud      = lp.cloud
  AND u.usage_unit = lp.usage_unit
  AND u.usage_end_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.billing_origin_product IN ('MODEL_SERVING', 'AI_GATEWAY')
  AND u.product_features.serving_type = 'FOUNDATION_MODEL'
GROUP BY ALL;
