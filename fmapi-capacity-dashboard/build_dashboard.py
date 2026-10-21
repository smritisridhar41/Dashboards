"""Builds fmapi_capacity_latency_errors.lvdash.json on top of the main.fmapi_observability data model."""
import json, sys

S = sys.argv[1] if len(sys.argv) > 1 else "main.fmapi_observability"
OUT = sys.argv[2] if len(sys.argv) > 2 else "fmapi_capacity_latency_errors.lvdash.json"

START = "CURRENT_TIMESTAMP() - MAKE_DT_INTERVAL(CAST(:lookback AS INT))"

# shared CTEs: latest capacity limit per workspace/model + workspace display label
LIM = f"""lim AS (
  SELECT workspace_id, model_key, itpm_limit, otpm_limit
  FROM {S}.capacity_limits
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, model_key ORDER BY effective_from DESC) = 1
),
wsn AS (
  SELECT w.workspace_id, COALESCE(c.workspace_name, w.workspace_name) AS workspace_name
  FROM {S}.workspaces w
  LEFT JOIN (SELECT workspace_id, MAX(workspace_name) AS workspace_name FROM {S}.capacity_limits GROUP BY 1) c
    USING (workspace_id)
)"""
WS_LABEL = "COALESCE(wsn.workspace_name, x.workspace_id)"

KPI_SQL = f"""WITH req AS (
  SELECT
    workspace_id,
    model_key,
    ANY_VALUE(model_provider)                                        AS model_provider,
    CONCAT_WS(', ', COLLECT_SET(endpoint_name))                      AS endpoints,
    COUNT(*)                                                         AS requests,
    COUNT_IF(status_code BETWEEN 200 AND 299)                        AS success_requests,
    COUNT_IF(status_code = 429)                                      AS errors_429,
    COUNT_IF(status_code BETWEEN 400 AND 499 AND status_code <> 429) AS errors_4xx,
    COUNT_IF(status_code >= 500)                                     AS errors_5xx,
    SUM(input_tokens)                                                AS input_tokens,
    SUM(cache_read_tokens)                                           AS cache_read_tokens,
    SUM(output_tokens)                                               AS output_tokens,
    PERCENTILE_APPROX(latency_ms, 0.50) / 1000.0                     AS latency_p50_s,
    PERCENTILE_APPROX(latency_ms, 0.95) / 1000.0                     AS latency_p95_s,
    PERCENTILE_APPROX(latency_ms, 0.99) / 1000.0                     AS latency_p99_s,
    PERCENTILE_APPROX(ttfb_ms, 0.95) / 1000.0                        AS ttfb_p95_s,
    PERCENTILE_APPROX(ttfb_ms, 0.99) / 1000.0                        AS ttfb_p99_s
  FROM {S}.requests
  WHERE event_time >= {START}
  GROUP BY ALL
),
mins AS (
  SELECT
    workspace_id,
    model_key,
    MAX(itpm)                       AS peak_itpm,
    PERCENTILE_APPROX(itpm, 0.99)   AS p99_itpm,
    PERCENTILE_APPROX(itpm, 0.95)   AS p95_itpm,
    MAX(otpm)                       AS peak_otpm,
    PERCENTILE_APPROX(otpm, 0.99)   AS p99_otpm,
    MAX(requests)                   AS peak_rpm
  FROM {S}.minute_metrics
  WHERE event_minute >= {START}
  GROUP BY ALL
),
{LIM},
x AS (
  SELECT
    r.*,
    m.peak_itpm, m.p99_itpm, m.p95_itpm, m.peak_otpm, m.p99_otpm, m.peak_rpm,
    l.itpm_limit, l.otpm_limit,
    m.peak_itpm / NULLIF(l.itpm_limit, 0)       AS peak_itpm_util,
    m.p99_itpm  / NULLIF(l.itpm_limit, 0)       AS p99_itpm_util,
    m.peak_otpm / NULLIF(l.otpm_limit, 0)       AS peak_otpm_util,
    r.success_requests / NULLIF(r.requests, 0)  AS success_rate,
    r.errors_429 / NULLIF(r.requests, 0)        AS rate_429,
    r.errors_5xx / NULLIF(r.requests, 0)        AS rate_5xx,
    r.cache_read_tokens / NULLIF(r.input_tokens, 0) AS cache_hit_rate
  FROM req r
  LEFT JOIN mins m USING (workspace_id, model_key)
  LEFT JOIN lim l  USING (workspace_id, model_key)
)
SELECT
  {WS_LABEL}                                  AS workspace,
  x.workspace_id,
  x.model_key                                 AS model,
  x.model_provider                            AS provider,
  CONCAT({WS_LABEL}, ' / ', x.model_key)      AS ws_model,
  x.* EXCEPT (workspace_id, model_key, model_provider),
  CASE
    WHEN x.itpm_limit IS NULL THEN '4 - No limit on file'
    WHEN GREATEST(x.peak_itpm_util, COALESCE(x.peak_otpm_util, 0)) >= 0.9 OR x.rate_429 >= 0.01
      THEN '1 - Request more capacity'
    WHEN GREATEST(x.peak_itpm_util, COALESCE(x.peak_otpm_util, 0)) >= 0.7 OR x.errors_429 > 0
      THEN '2 - Watch'
    ELSE '3 - Healthy'
  END                                         AS capacity_status,
  CEIL(x.peak_itpm * 1.3 / 1e6) * 1e6         AS recommended_itpm,
  CASE WHEN x.itpm_limit IS NOT NULL THEN GREATEST(CEIL(x.peak_itpm * 1.3 / 1e6) * 1e6 - x.itpm_limit, 0) END AS additional_itpm_needed,
  CEIL(x.peak_otpm * 1.3 / 1e5) * 1e5         AS recommended_otpm,
  CASE WHEN x.otpm_limit IS NOT NULL THEN GREATEST(CEIL(x.peak_otpm * 1.3 / 1e5) * 1e5 - x.otpm_limit, 0) END AS additional_otpm_needed
FROM x
LEFT JOIN wsn USING (workspace_id)"""

HOURLY_SQL = f"""WITH {LIM}
SELECT
  {WS_LABEL}                                  AS workspace,
  x.model_key                                 AS model,
  x.model_provider                            AS provider,
  CONCAT({WS_LABEL}, ' / ', x.model_key)      AS ws_model,
  x.* EXCEPT (model_key, model_provider),
  l.itpm_limit,
  l.otpm_limit,
  x.peak_itpm / NULLIF(l.itpm_limit, 0)       AS itpm_util,
  x.peak_otpm / NULLIF(l.otpm_limit, 0)       AS otpm_util,
  (x.requests - x.success_requests) / NULLIF(x.requests, 0) AS error_rate,
  x.latency_p50_ms / 1000.0                   AS latency_p50_s,
  x.latency_p95_ms / 1000.0                   AS latency_p95_s,
  x.latency_p99_ms / 1000.0                   AS latency_p99_s,
  x.ttfb_p95_ms / 1000.0                      AS ttfb_p95_s,
  x.ttfb_p99_ms / 1000.0                      AS ttfb_p99_s
FROM {S}.hourly_metrics x
LEFT JOIN lim l USING (workspace_id, model_key)
LEFT JOIN wsn USING (workspace_id)
WHERE x.event_hour >= DATE_TRUNC('HOUR', {START})"""

ERRORS_SQL = f"""WITH {LIM}
SELECT
  {WS_LABEL}                                  AS workspace,
  x.model_key                                 AS model,
  CONCAT({WS_LABEL}, ' / ', x.model_key)      AS ws_model,
  x.event_hour,
  x.status_code,
  x.status_class,
  COUNT(*)                                    AS requests
FROM {S}.requests x
LEFT JOIN wsn USING (workspace_id)
WHERE x.event_time >= {START}
GROUP BY ALL"""

CALLERS_SQL = f"""WITH {LIM}
SELECT
  {WS_LABEL}                                  AS workspace,
  x.model_key                                 AS model,
  x.requester,
  x.requester_type,
  COUNT(*)                                    AS requests,
  COUNT_IF(x.status_code = 429)               AS errors_429,
  COUNT_IF(x.status_code >= 500)              AS errors_5xx,
  COUNT_IF(x.status_code = 429) / COUNT(*)    AS rate_429,
  SUM(x.input_tokens + x.output_tokens)       AS total_tokens,
  PERCENTILE_APPROX(x.latency_ms, 0.95) / 1000.0 AS latency_p95_s
FROM {S}.requests x
LEFT JOIN wsn USING (workspace_id)
WHERE x.event_time >= {START}
GROUP BY ALL"""

SPEND_SQL = f"""WITH {LIM}
SELECT
  {WS_LABEL}                                  AS workspace,
  x.model_key                                 AS model,
  x.usage_date,
  x.endpoint_name,
  x.dbus,
  x.list_cost_usd
FROM {S}.daily_model_spend x
LEFT JOIN wsn USING (workspace_id)
WHERE x.usage_date >= CAST({START} AS DATE)"""

TOTALS_SQL = f"""SELECT
  COUNT(*)                                                AS requests,
  COUNT_IF(status_code BETWEEN 200 AND 299) / COUNT(*)    AS success_rate,
  COUNT_IF(status_code = 429)                             AS errors_429,
  COUNT_IF(status_code >= 500)                            AS errors_5xx,
  PERCENTILE_APPROX(latency_ms, 0.95) / 1000.0            AS latency_p95_s,
  PERCENTILE_APPROX(latency_ms, 0.99) / 1000.0            AS latency_p99_s,
  SUM(input_tokens + output_tokens)                       AS total_tokens,
  CONCAT(CAST(MIN(event_date) AS STRING), '  →  ', CAST(MAX(event_date) AS STRING)) AS date_range
FROM {S}.requests
WHERE event_time >= {START}"""

LOOKBACK_PARAM = {
    "displayName": "Days Lookback", "keyword": "lookback", "dataType": "STRING",
    "defaultSelection": {"values": {"dataType": "STRING", "values": [{"value": "7"}]}},
}


def ds(name, display, sql, param=True):
    d = {"name": name, "displayName": display, "queryLines": [l + "\n" for l in sql.split("\n")]}
    if param:
        d["parameters"] = [LOOKBACK_PARAM]
    return d


datasets = [
    ds("lookback_options", "Lookback Options", "SELECT * FROM VALUES ('1'), ('7'), ('14'), ('30'), ('90') AS t(days)", False),
    ds("kpi", "Capacity KPIs by Workspace / Model", KPI_SQL),
    ds("hourly", "Hourly Metrics", HOURLY_SQL),
    ds("errors", "Requests by Status Code (Hourly)", ERRORS_SQL),
    ds("callers", "Usage by Caller", CALLERS_SQL),
    ds("spend", "Daily Spend by Model", SPEND_SQL),
    ds("totals", "Totals", TOTALS_SQL),
]
PARAM_DS = [d["name"] for d in datasets if "parameters" in d]
FILTER_DS = ["kpi", "hourly", "errors", "callers", "spend"]   # datasets carrying workspace/model columns

# ---------------------------------------------------------------- widget helpers
def fld(name, expr=None):
    return {"name": name, "expression": expr or f"`{name}`"}

def q(dataset, fields, disagg=True, orders=None):
    qq = {"datasetName": dataset, "fields": fields, "disaggregated": disagg}
    if orders:
        qq["orders"] = orders
    return [{"name": "main_query", "query": qq}]

def pos(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}

def text(name, lines, p):
    return {"widget": {"name": name, "multilineTextboxSpec": {"lines": lines}}, "position": p}

PCT = lambda places=1: {"type": "number-percent", "decimalPlaces": {"type": "exact", "places": places}}
NUM = lambda places=0, abbr="compact": {"type": "number-plain", "abbreviation": abbr,
                                        "decimalPlaces": {"type": "exact", "places": places}}
SEC = {"type": "number-plain", "abbreviation": "none", "decimalPlaces": {"type": "exact", "places": 2}}

UTIL_RULES = {"type": "basic", "rules": [
    {"condition": {"operand": {"type": "data-value", "value": "1"}, "operator": ">="}, "backgroundColor": "#EF4444", "foregroundColor": "#FFFFFF"},
    {"condition": {"operand": {"type": "data-value", "value": "0.9"}, "operator": ">="}, "backgroundColor": "#FB923C"},
    {"condition": {"operand": {"type": "data-value", "value": "0.7"}, "operator": ">="}, "backgroundColor": "#FDE68A"},
    {"condition": {"operand": {"type": "data-value", "value": "0.7"}, "operator": "<"}, "backgroundColor": "#4ADE80"}]}
ERR_RULES = {"type": "basic", "rules": [
    {"condition": {"operand": {"type": "data-value", "value": "0.05"}, "operator": ">"}, "backgroundColor": "#EF4444", "foregroundColor": "#FFFFFF"},
    {"condition": {"operand": {"type": "data-value", "value": "0.01"}, "operator": ">"}, "backgroundColor": "#FB923C"},
    {"condition": {"operand": {"type": "data-value", "value": "0"}, "operator": ">"}, "backgroundColor": "#FDE68A"}]}

def col(field, display, fmt=None, width=None, style=None, search=False):
    c = {"fieldName": field, "displayName": display}
    if fmt: c["format"] = fmt
    if width: c["columnWidth"] = width
    if style: c["style"] = style
    if search: c["useForSearch"] = True
    return c

def table(name, title, dataset, cols, p, orders=None, desc=None):
    frame = {"showTitle": True, "title": title}
    if desc:
        frame.update(showDescription=True, description=desc)
    return {"widget": {"name": name,
                       "queries": q(dataset, [fld(c["fieldName"]) for c in cols], True, orders),
                       "spec": {"version": 2, "widgetType": "table", "frame": frame,
                                "encodings": {"columns": cols}, "displayRowNumbers": False,
                                "bandedRows": {"enabled": True}, "data": {"queryName": "main_query"}}},
            "position": p}

def counter(name, title, dataset, field, fmt, p):
    return {"widget": {"name": name, "queries": q("totals" if dataset is None else dataset, [fld(field)]),
                       "spec": {"version": 2, "widgetType": "counter", "frame": {"showTitle": True, "title": title},
                                "encodings": {"value": {"fieldName": field, **({"format": fmt} if fmt else {})}},
                                "data": {"queryName": "main_query"}}},
            "position": p}

def chart(name, kind, title, dataset, x, y, ydisp, p, color="ws_model", yfmt=None, stack=False, agg=None):
    """Line/bar chart: x temporal, y one field, colored by series. agg='SUM'/'MAX' aggregates in the widget."""
    if agg:
        yname = f"{agg.lower()}({y})"
        fields = [fld(x), fld(yname, f"{agg}(`{y}`)")] + ([fld(color)] if color else [])
    else:
        yname = y
        fields = [fld(x), fld(y)] + ([fld(color)] if color else [])
    enc = {"x": {"fieldName": x, "displayName": "Hour" if "hour" in x else "Date", "scale": {"type": "temporal"}},
           "y": {"fieldName": yname, "displayName": ydisp, "scale": {"type": "quantitative"},
                 **({"format": yfmt} if yfmt else {})}}
    if color:
        enc["color"] = {"fieldName": color, "displayName": "Workspace / Model" if color == "ws_model" else color,
                        "scale": {"type": "categorical"}}
    spec = {"version": 3, "widgetType": kind, "frame": {"showTitle": True, "title": title},
            "encodings": enc, "data": {"queryName": "main_query"}}
    if stack:
        spec["mark"] = {"layout": "stack"}
    return {"widget": {"name": name, "queries": q(dataset, fields, agg is None), "spec": spec}, "position": p}

def multi_line(name, title, dataset, x, ys, p, yfmt=None):
    fields = [fld(x)] + [fld(f) for f, _ in ys]
    spec = {"version": 3, "widgetType": "line", "frame": {"showTitle": True, "title": title},
            "encodings": {"x": {"fieldName": x, "displayName": "Hour", "scale": {"type": "temporal"}},
                          "y": {"fields": [{"fieldName": f, "displayName": d} for f, d in ys],
                                "scale": {"type": "quantitative"}, **({"format": yfmt} if yfmt else {})}},
            "data": {"queryName": "main_query"}}
    return {"widget": {"name": name, "queries": q(dataset, fields), "spec": spec}, "position": p}

def field_filter(name, title, field, datasets_, p, single=False):
    queries, enc = [], []
    for d in datasets_:
        qn = f"f_{name}_{d}"
        queries.append({"name": qn, "query": {"datasetName": d, "fields": [
            fld(field), fld(f"{field}_associativity", "COUNT_IF(`associative_filter_predicate_group`)")],
            "disaggregated": False}})
        enc.append({"fieldName": field, "queryName": qn})
    return {"widget": {"name": name, "queries": queries,
                       "spec": {"version": 2, "widgetType": "filter-single-select" if single else "filter-multi-select",
                                "frame": {"showTitle": True, "title": title}, "encodings": {"fields": enc}}},
            "position": p}

def lookback_filter(p, name="global_lookback"):
    queries = [{"name": "f_lookback_options", "query": {"datasetName": "lookback_options", "fields": [
        fld("days"), fld("days_associativity", "COUNT_IF(`associative_filter_predicate_group`)")], "disaggregated": False}}]
    enc = [{"fieldName": "days", "displayName": "days", "queryName": "f_lookback_options"}]
    for d in PARAM_DS:
        qn = f"p_lookback_{d}"
        queries.append({"name": qn, "query": {"datasetName": d, "parameters": [{"name": "lookback", "keyword": "lookback"}],
                                              "disaggregated": False}})
        enc.append({"parameterName": "lookback", "queryName": qn})
    return {"widget": {"name": name, "queries": queries,
                       "spec": {"version": 2, "widgetType": "filter-single-select",
                                "frame": {"showTitle": True, "title": "Days Lookback"}, "encodings": {"fields": enc}}},
            "position": p}

def header(prefix, title, subtitle):
    return [
        text(f"{prefix}_title", [f"## {title}\n", "\n", subtitle], pos(0, 0, 8, 2)),
        counter(f"{prefix}_range", "Data Date Range", "totals", "date_range", None, pos(8, 0, 4, 2)),
    ]

# ---------------------------------------------------------------- page 1: capacity KPIs
STATUS_RULES = {"type": "basic", "rules": [
    {"condition": {"operand": {"type": "data-value", "value": "1 - Request more capacity"}, "operator": "="}, "backgroundColor": "#EF4444", "foregroundColor": "#FFFFFF"},
    {"condition": {"operand": {"type": "data-value", "value": "2 - Watch"}, "operator": "="}, "backgroundColor": "#FDE68A"},
    {"condition": {"operand": {"type": "data-value", "value": "3 - Healthy"}, "operator": "="}, "backgroundColor": "#4ADE80"}]}

kpi_cols = [
    col("workspace", "Workspace", width=130, search=True),
    col("model", "Model", width=170, search=True),
    col("provider", "Provider", width=90),
    col("capacity_status", "Capacity Status", width=170, style=STATUS_RULES),
    col("itpm_limit", "ITPM Limit", NUM(1), 80),
    col("peak_itpm", "Peak ITPM", NUM(1), 80),
    col("p99_itpm", "P99 ITPM", NUM(1), 80),
    col("peak_itpm_util", "Peak ITPM Util %", PCT(), 85, UTIL_RULES),
    col("p99_itpm_util", "P99 ITPM Util %", PCT(), 85, UTIL_RULES),
    col("otpm_limit", "OTPM Limit", NUM(1), 80),
    col("peak_otpm", "Peak OTPM", NUM(1), 80),
    col("peak_otpm_util", "Peak OTPM Util %", PCT(), 85, UTIL_RULES),
    col("peak_rpm", "Peak RPM", NUM(1), 75),
    col("requests", "Requests", NUM(1), 80),
    col("success_rate", "Success %", PCT(2), 80),
    col("errors_429", "429s", NUM(0), 65),
    col("rate_429", "429 Rate", PCT(2), 70, ERR_RULES),
    col("errors_5xx", "5xx", NUM(0), 60),
    col("rate_5xx", "5xx Rate", PCT(2), 70, ERR_RULES),
    col("latency_p50_s", "Latency p50 (s)", SEC, 80),
    col("latency_p95_s", "Latency p95 (s)", SEC, 80),
    col("latency_p99_s", "Latency p99 (s)", SEC, 80),
    col("ttfb_p95_s", "TTFT p95 (s)", SEC, 75),
    col("ttfb_p99_s", "TTFT p99 (s)", SEC, 75),
    col("cache_hit_rate", "Cache Hit %", PCT(), 75),
    col("recommended_itpm", "Recommended ITPM", NUM(1), 95),
    col("additional_itpm_needed", "Additional ITPM Needed", NUM(1), 100),
    col("endpoints", "Endpoints", width=220),
]
needs_cols = [
    col("capacity_status", "Capacity Status", width=180, style=STATUS_RULES),
    col("workspace", "Workspace", width=130),
    col("model", "Model", width=170),
    col("itpm_limit", "ITPM Limit", NUM(1), 90),
    col("peak_itpm", "Peak ITPM", NUM(1), 90),
    col("peak_itpm_util", "Peak ITPM Util %", PCT(), 90, UTIL_RULES),
    col("recommended_itpm", "Recommended ITPM", NUM(1), 110),
    col("additional_itpm_needed", "Additional ITPM Needed", NUM(1), 120),
    col("otpm_limit", "OTPM Limit", NUM(1), 90),
    col("peak_otpm", "Peak OTPM", NUM(1), 90),
    col("recommended_otpm", "Recommended OTPM", NUM(1), 110),
    col("additional_otpm_needed", "Additional OTPM Needed", NUM(1), 120),
    col("errors_429", "429s", NUM(0), 70),
    col("rate_429", "429 Rate", PCT(2), 80, ERR_RULES),
]
ORDER_STATUS = [{"direction": "ASC", "expression": "`capacity_status`"}, {"direction": "DESC", "expression": "`peak_itpm_util`"}]

page_kpi = header("kpi", "Model Capacity, Latency & Errors",
                  "Per workspace and model over the selected lookback. Utilization compares the busiest minute to the limits in `capacity_limits`.") + [
    counter("c_requests", "Requests", "totals", "requests", NUM(1), pos(0, 2, 2, 2)),
    counter("c_success", "Success Rate", "totals", "success_rate", PCT(2), pos(2, 2, 2, 2)),
    counter("c_429", "429 Errors", "totals", "errors_429", NUM(0), pos(4, 2, 2, 2)),
    counter("c_5xx", "5xx Errors", "totals", "errors_5xx", NUM(0), pos(6, 2, 2, 2)),
    counter("c_p95", "Latency p95 (s)", "totals", "latency_p95_s", SEC, pos(8, 2, 2, 2)),
    counter("c_p99", "Latency p99 (s)", "totals", "latency_p99_s", SEC, pos(10, 2, 2, 2)),
    lookback_filter(pos(0, 4, 2, 1)),
    field_filter("kpi_ws", "Workspace", "workspace", FILTER_DS, pos(2, 4, 3, 1)),
    field_filter("kpi_model", "Model", "model", FILTER_DS, pos(5, 4, 4, 1)),
    field_filter("kpi_provider", "Provider", "provider", ["kpi", "hourly"], pos(9, 4, 3, 1)),
    table("kpi_table", "Capacity KPIs by Workspace / Model", "kpi", kpi_cols, pos(0, 5, 12, 12), ORDER_STATUS,
          "Util %: red >=100% · orange >=90% · yellow >=70% · green <70%. "
          "Status: 'Request more capacity' = peak util >=90% or 429 rate >=1%; 'Watch' = peak util >=70% or any 429s. "
          "Recommended = peak x 1.3 (30% headroom), rounded up."),
    table("needs_table", "Capacity Actions (sorted by urgency)", "kpi", needs_cols, pos(0, 17, 12, 7), ORDER_STATUS,
          "Use the Additional ITPM/OTPM Needed columns when requesting a limit increase from your Databricks account team."),
]

# ---------------------------------------------------------------- page 2: time series
page_ts = header("ts", "Capacity & Latency Over Time", "Hourly. Peaks are the busiest minute in each hour.") + [
    lookback_filter(pos(0, 2, 2, 1), "ts_lookback"),
    field_filter("ts_ws", "Workspace", "workspace", FILTER_DS, pos(2, 2, 3, 1)),
    field_filter("ts_model", "Model", "model", FILTER_DS, pos(5, 2, 4, 1)),
    field_filter("ts_one", "Single model (for limit lines)", "ws_model", ["hourly"], pos(0, 3, 6, 1), single=True),
    multi_line("ts_itpm_limit", "Peak ITPM vs Limit (select a single model above)", "hourly", "event_hour",
               [("peak_itpm", "Peak ITPM"), ("peak_uncached_itpm", "Peak uncached ITPM"), ("itpm_limit", "ITPM Limit")],
               pos(0, 4, 6, 6), NUM(1)),
    multi_line("ts_otpm_limit", "Peak OTPM vs Limit (select a single model above)", "hourly", "event_hour",
               [("peak_otpm", "Peak OTPM"), ("otpm_limit", "OTPM Limit")], pos(6, 4, 6, 6), NUM(1)),
    chart("ts_util", "line", "Peak ITPM Utilization %", "hourly", "event_hour", "itpm_util", "ITPM Util %", pos(0, 10, 12, 6), yfmt=PCT()),
    chart("ts_itpm", "line", "Peak ITPM by Model", "hourly", "event_hour", "peak_itpm", "Peak ITPM", pos(0, 16, 6, 6), yfmt=NUM(1)),
    chart("ts_rpm", "line", "Peak RPM by Model", "hourly", "event_hour", "peak_rpm", "Peak RPM", pos(6, 16, 6, 6), yfmt=NUM(1)),
    chart("ts_p95", "line", "Latency p95 (s)", "hourly", "event_hour", "latency_p95_s", "p95 (s)", pos(0, 22, 6, 6), yfmt=SEC),
    chart("ts_p99", "line", "Latency p99 (s)", "hourly", "event_hour", "latency_p99_s", "p99 (s)", pos(6, 22, 6, 6), yfmt=SEC),
    chart("ts_ttft95", "line", "Time to First Token p95 (s)", "hourly", "event_hour", "ttfb_p95_s", "TTFT p95 (s)", pos(0, 28, 6, 6), yfmt=SEC),
    chart("ts_ttft99", "line", "Time to First Token p99 (s)", "hourly", "event_hour", "ttfb_p99_s", "TTFT p99 (s)", pos(6, 28, 6, 6), yfmt=SEC),
]

# ---------------------------------------------------------------- page 3: errors
err_code_cols = [
    col("workspace", "Workspace", width=140), col("model", "Model", width=180),
    col("status_class", "Status Class", width=160), col("status_code", "Status Code", NUM(0, "none"), 90),
    col("sum(requests)", "Requests", NUM(0), 100),
]
caller_cols = [
    col("workspace", "Workspace", width=130), col("model", "Model", width=170),
    col("requester", "Caller", width=260, search=True), col("requester_type", "Caller Type", width=120),
    col("requests", "Requests", NUM(1), 90), col("errors_429", "429s", NUM(0), 70),
    col("rate_429", "429 Rate", PCT(2), 80, ERR_RULES), col("errors_5xx", "5xx", NUM(0), 60),
    col("total_tokens", "Tokens", NUM(1), 90), col("latency_p95_s", "Latency p95 (s)", SEC, 90),
]

def err_table(p):
    fields = [fld("workspace"), fld("model"), fld("status_class"), fld("status_code"), fld("sum(requests)", "SUM(`requests`)")]
    return {"widget": {"name": "err_codes", "queries": q("errors", fields, False,
                                                       [{"direction": "DESC", "expression": "SUM(`requests`)"}]),
                       "spec": {"version": 2, "widgetType": "table", "frame": {"showTitle": True, "title": "Requests by Status Code"},
                                "encodings": {"columns": err_code_cols}, "bandedRows": {"enabled": True},
                                "data": {"queryName": "main_query"}}}, "position": p}

page_err = header("err", "Errors", "Status codes per workspace and model. 429 = rate limited (FMAPI capacity or your AI Gateway rate limits).") + [
    lookback_filter(pos(0, 2, 2, 1), "err_lookback"),
    field_filter("err_ws", "Workspace", "workspace", FILTER_DS, pos(2, 2, 3, 1)),
    field_filter("err_model", "Model", "model", FILTER_DS, pos(5, 2, 4, 1)),
    chart("err_by_class", "bar", "Requests by Status Class (Hourly)", "errors", "event_hour", "requests", "Requests",
          pos(0, 3, 12, 6), color="status_class", yfmt=NUM(1), stack=True, agg="SUM"),
    chart("err_429", "bar", "429 Errors by Model (Hourly)", "hourly", "event_hour", "errors_429", "429s",
          pos(0, 9, 6, 6), yfmt=NUM(0), stack=True),
    chart("err_5xx", "bar", "5xx Errors by Model (Hourly)", "hourly", "event_hour", "errors_5xx", "5xx",
          pos(6, 9, 6, 6), yfmt=NUM(0), stack=True),
    chart("err_rate", "line", "Error Rate % by Model (Hourly)", "hourly", "event_hour", "error_rate", "Error rate",
          pos(0, 15, 12, 5), yfmt=PCT(2)),
    err_table(pos(0, 20, 6, 8)),
    table("callers_table", "Usage & Errors by Caller", "callers", caller_cols, pos(6, 20, 6, 8),
          [{"direction": "DESC", "expression": "`requests`"}]),
]

# ---------------------------------------------------------------- page 4: spend
spend_cols = [col("workspace", "Workspace", width=150), col("model", "Model", width=200),
              col("sum(list_cost_usd)", "List Cost (USD)", {"type": "number-currency", "currencyCode": "USD",
                                                            "abbreviation": "compact", "decimalPlaces": {"type": "exact", "places": 1}}, 120),
              col("sum(dbus)", "DBUs", NUM(1), 100)]

def spend_table(p):
    fields = [fld("workspace"), fld("model"), fld("sum(list_cost_usd)", "SUM(`list_cost_usd`)"), fld("sum(dbus)", "SUM(`dbus`)")]
    return {"widget": {"name": "spend_table", "queries": q("spend", fields, False,
                                                         [{"direction": "DESC", "expression": "SUM(`list_cost_usd`)"}]),
                       "spec": {"version": 2, "widgetType": "table", "frame": {"showTitle": True, "title": "Spend by Workspace / Model"},
                                "encodings": {"columns": spend_cols}, "bandedRows": {"enabled": True},
                                "data": {"queryName": "main_query"}}}, "position": p}

page_spend = header("sp", "Spend", "List-price cost of Foundation Model API usage from system.billing.usage.") + [
    lookback_filter(pos(0, 2, 2, 1), "sp_lookback"),
    field_filter("sp_ws", "Workspace", "workspace", FILTER_DS, pos(2, 2, 3, 1)),
    field_filter("sp_model", "Model", "model", FILTER_DS, pos(5, 2, 4, 1)),
    chart("sp_daily", "bar", "Daily List Cost by Model", "spend", "usage_date", "list_cost_usd", "List cost (USD)",
          pos(0, 3, 12, 7), color="model", stack=True, agg="SUM",
          yfmt={"type": "number-currency", "currencyCode": "USD", "abbreviation": "compact"}),
    spend_table(pos(0, 10, 12, 8)),
]

# ---------------------------------------------------------------- page 5: README
readme = [
    "## About this dashboard\n", "\n",
    "Built entirely on your account's system tables, so it reflects only your own traffic. "
    "`system.ai_gateway.usage` records request metadata (tokens, status, latency) but never prompts or responses, "
    "so it works in Zero Data Retention workspaces.\n", "\n",
    "### Data model (`main.fmapi_observability`, refreshed hourly)\n",
    "- `requests`: one row per model request (from `system.ai_gateway.usage`)\n",
    "- `minute_metrics`: tokens and requests per minute, per workspace and model\n",
    "- `hourly_metrics`: hourly peaks, error counts, latency and TTFT percentiles\n",
    "- `daily_model_spend`: daily DBUs and list cost per model (from `system.billing.usage`)\n",
    "- `capacity_limits`: **maintained by you**. Add a row with the ITPM/OTPM allocation for each workspace and model, "
    "and a new row whenever a limit changes.\n", "\n",
    "### Metric definitions\n",
    "- **ITPM**: input tokens per minute, including cached input (what FMAPI input limits count). "
    "**Uncached ITPM** excludes cache reads and writes.\n",
    "- **Peak**: busiest single minute in the window. **P99 ITPM**: 99th percentile of active minutes, "
    "less sensitive to one-off spikes.\n",
    "- **Util %**: peak (or P99) tokens per minute divided by the limit in `capacity_limits`.\n",
    "- **Latency**: end-to-end request time. **TTFT**: time to first token (first byte for streaming).\n",
    "- **Capacity Status**: *Request more capacity* = peak util >=90% or 429 rate >=1%; *Watch* = peak util >=70% or any 429s; "
    "*No limit on file* = add the allocation to `capacity_limits`.\n",
    "- **Recommended ITPM/OTPM**: observed peak x 1.3 (30% headroom), rounded up. **Additional needed** = recommended minus current limit.\n", "\n",
    "### Notes\n",
    "- 429s can come from FMAPI capacity limits or from AI Gateway rate limits you have set on an endpoint.\n",
    "- Tokens are attributed to the minute the request was logged, so very long streaming requests count toward the minute they finish.\n",
    "- Spend uses list prices; your contracted rates may differ.\n",
]
page_readme = [text("readme", readme, pos(0, 0, 12, 14))]

# ---------------------------------------------------------------- assemble
pages = [
    {"name": "capacity_kpis", "displayName": "Capacity KPIs", "layout": page_kpi, "pageType": "PAGE_TYPE_CANVAS"},
    {"name": "time_series", "displayName": "Capacity & Latency Over Time", "layout": page_ts, "pageType": "PAGE_TYPE_CANVAS"},
    {"name": "errors", "displayName": "Errors", "layout": page_err, "pageType": "PAGE_TYPE_CANVAS"},
    {"name": "spend", "displayName": "Spend", "layout": page_spend, "pageType": "PAGE_TYPE_CANVAS"},
    {"name": "readme", "displayName": "README", "layout": page_readme, "pageType": "PAGE_TYPE_CANVAS"},
]
dash = {"datasets": datasets, "pages": pages, "uiSettings": {"theme": {"widgetHeaderAlignment": "ALIGNMENT_UNSPECIFIED"}}}
json.dump(dash, open(OUT, "w"), indent=2)
print("wrote", OUT, len(datasets), "datasets", sum(len(p["layout"]) for p in pages), "widgets")
