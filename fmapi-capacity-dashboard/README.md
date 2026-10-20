# FMAPI Capacity, Latency & Errors dash

An AI/BI dashboard for Foundation Model API (FMAPI) and AI Gateway traffic. For each workspace and model it shows:

- **Capacity:** peak and P99 input/output tokens per minute (ITPM / OTPM) against your allocated limits, plus a recommended limit and how much more to request
- **Errors:** 429 (rate limited), 4xx and 5xx counts and rates, by model and by caller
- **Latency:** p50 / p95 / p99 end-to-end latency and time to first token (TTFT)
- **Spend:** daily list cost per model

It is built only on **system tables** (`system.ai_gateway.usage`, `system.billing.*`, `system.access.workspaces_latest`). These record request metadata such as tokens, status codes and latency, never prompts or responses, so the dashboard also works for **Zero Data Retention (ZDR)** workspaces and needs no inference tables or setup inside the serving workspaces.

## Contents

| File | What it is |
|---|---|
| `fmapi_observability_data_model.sql` | Creates the schema, the `capacity_limits` table and 5 materialized views (refreshed hourly) |
| `fmapi_capacity_latency_errors.lvdash.json` | The dashboard. Import it into Databricks |
| `build_dashboard.py` | Regenerates the dashboard JSON, e.g. to point it at a different catalog/schema |

## Dashboard pages

1. **Capacity KPIs:** headline counters (requests, success rate, 429s, 5xx, p95/p99 latency), a per workspace/model KPI table, and a **Capacity Actions** table sorted by urgency
2. **Capacity & Latency Over Time:** peak ITPM/OTPM vs limit, utilization %, peak RPM, latency and TTFT percentiles (hourly)
3. **Errors:** requests by status class, 429s and 5xx by model, error rate %, status codes, and usage and errors by caller
4. **Spend:** daily list cost by model and spend by workspace/model
5. **README:** metric definitions inside the dashboard

Every page has filters for **Days Lookback**, **Workspace** and **Model**.

---

## Prerequisites

- Unity Catalog enabled, and **AI Gateway / FMAPI usage** flowing to `system.ai_gateway.usage`
- Access to the system schemas `system.ai_gateway`, `system.billing` and `system.access`. An account admin may need to grant this:
  ```sql
  GRANT USE CATALOG ON CATALOG system TO `<group>`;
  GRANT USE SCHEMA, SELECT ON SCHEMA system.ai_gateway TO `<group>`;
  GRANT USE SCHEMA, SELECT ON SCHEMA system.billing   TO `<group>`;
  GRANT USE SCHEMA, SELECT ON SCHEMA system.access    TO `<group>`;
  ```
- A **serverless SQL warehouse** (materialized views with `SCHEDULE` require serverless)
- `CREATE SCHEMA` on the target catalog (default `main`)

Quick check that data is there:
```sql
SELECT destination_model, COUNT(*) AS requests, MIN(event_time), MAX(event_time)
FROM system.ai_gateway.usage
WHERE event_time >= current_timestamp() - INTERVAL 7 DAYS
GROUP BY 1 ORDER BY 2 DESC;
```

---

## Step-by-step setup

### Step 1: Choose a catalog and schema
The default is `main.fmapi_observability`. To use another location, find/replace `main.fmapi_observability` in `fmapi_observability_data_model.sql`, and regenerate the dashboard JSON (Step 5).

### Step 2: Create the data model
1. Open **SQL Editor** in your workspace and select a **serverless SQL warehouse**.
2. Paste the contents of `fmapi_observability_data_model.sql` and click **Run all**.
3. This creates:

   | Object | Type | Purpose |
   |---|---|---|
   | `capacity_limits` | Table (manual) | Your ITPM / OTPM allocation per workspace + model |
   | `workspaces` | Materialized view | Workspace ID to name |
   | `requests` | Materialized view | One row per model request (status, tokens, cache tokens, latency, TTFT) |
   | `minute_metrics` | Materialized view | Tokens / requests / 429s / 5xx per minute (basis for TPM peaks) |
   | `hourly_metrics` | Materialized view | Hourly peaks, error counts, latency and TTFT percentiles |
   | `daily_model_spend` | Materialized view | Daily DBUs and list cost per model |

4. The first refresh can take a few minutes, depending on traffic volume. After that the views refresh **every hour**.

### Step 3: Enter your capacity limits
Workspace-level FMAPI limits are not exposed in system tables, so you enter them in `capacity_limits`. Add one row per workspace + model, and a **new row** whenever a limit changes. The dashboard uses the latest `effective_from`.

```sql
INSERT INTO main.fmapi_observability.capacity_limits VALUES
  ('<workspace_id>', 'Production', 'claude-sonnet-5-5', 10000000, 2000000, DATE'2026-10-01', '<ticket>', ''),
  ('<workspace_id>', 'Production', 'gpt-6-1-sol',        5000000, 1000000, DATE'2026-10-01', '<ticket>', ''),
  ('<workspace_id>', 'Dev',        'claude-opus-5-5',    2000000,    NULL, DATE'2026-10-01', '<ticket>', '');
```

- `model_key` = the model name in lower case, with spaces and dots replaced by dashes, e.g. `claude-opus-5-5`, `gemini-3-5-flash-lite`. To see the exact keys in your data:
  ```sql
  SELECT DISTINCT workspace_id, model_key FROM main.fmapi_observability.requests;
  ```
- `otpm_limit` can be `NULL` if there is no output limit.
- Models without a row show **"4 - No limit on file"** in the dashboard. Everything else still works for them.

### Step 4: Import the dashboard
1. Go to **Dashboards → Create dashboard → Import dashboard from file** (or use the kebab menu **⋮ → Import**).
2. Select `fmapi_capacity_latency_errors.lvdash.json`.
3. When prompted, pick your **serverless SQL warehouse**.
4. Click **Publish**. Choose *Embed credentials* if viewers should not need direct access to the system tables.
5. (Optional) **Schedule** an email subscription, e.g. a daily snapshot of the Capacity KPIs page.

### Step 5 (optional): Regenerate the dashboard for another schema
```bash
python3 build_dashboard.py <catalog>.<schema> fmapi_capacity_latency_errors.lvdash.json
```
Then re-import the generated JSON (Step 4).

---

## How to read it

### Capacity Status (Capacity KPIs page)
| Status | Rule | Action |
|---|---|---|
| **1 - Request more capacity** | Peak ITPM/OTPM utilization ≥ 90% **or** 429 rate ≥ 1% | Open a capacity request for the "Additional needed" amount |
| **2 - Watch** | Utilization ≥ 70% **or** any 429s | Monitor; prepare a request if growth continues |
| **3 - Healthy** | Below both thresholds | No action |
| **4 - No limit on file** | No row in `capacity_limits` | Add the allocation (Step 3) |

### Metric definitions
- **ITPM:** input tokens per minute, *including cached input* (what FMAPI input limits count). **Uncached ITPM** excludes cache reads and writes.
- **Peak:** the busiest single minute in the window. **P99 ITPM:** 99th percentile of active minutes, less sensitive to one-off spikes.
- **Util %:** peak (or P99) tokens per minute ÷ the limit in `capacity_limits`.
- **Recommended ITPM/OTPM:** observed peak × 1.3 (30% headroom), rounded up. **Additional needed** = recommended − current limit (blank when no limit is on file).
- **Latency:** end-to-end request time. **TTFT:** time to first token (first byte for streaming requests).
- **Spend:** list price from `system.billing.list_prices`. Your contracted rates may differ.

### Using it for a capacity request
1. Set **Days Lookback** to cover a representative peak period (e.g. 14 or 30 days).
2. On **Capacity KPIs**, sort **Capacity Actions** and note the models marked *Request more capacity* or *Watch*.
3. On **Capacity & Latency Over Time**, select a single model to see peak ITPM/OTPM against the limit line and confirm the peaks are sustained, not one-off bursts.
4. Check **Errors**: rising 429s alongside high utilization confirm a capacity constraint.
5. Use **Recommended** / **Additional needed** ITPM and OTPM as the request size.

---

## Notes and limitations
- 429s can come from FMAPI capacity limits **or** from AI Gateway rate limits configured on an endpoint.
- Tokens are attributed to the minute the request was logged, so long streaming requests count toward the minute they finish.
- System table data typically lands within minutes to an hour. Combined with the hourly materialized view refresh, the dashboard can lag real time by up to about 2 hours.
- Only traffic with a `destination_model` is included (model calls, not MCP or other gateway traffic).

## Troubleshooting
| Symptom | Fix |
|---|---|
| `TABLE_OR_VIEW_NOT_FOUND system.ai_gateway.usage` | Ask an account admin to grant access to the `system.ai_gateway` schema (Prerequisites) |
| Materialized view creation fails | Use a **serverless** SQL warehouse |
| Dashboard is empty | Check the Days Lookback filter; run `SELECT COUNT(*) FROM main.fmapi_observability.requests` |
| Every model shows "No limit on file" | Make sure `model_key` / `workspace_id` in `capacity_limits` exactly match the values in `requests` |
| Need fresher data | `REFRESH MATERIALIZED VIEW main.fmapi_observability.requests;` (then `minute_metrics`, `hourly_metrics`) |
