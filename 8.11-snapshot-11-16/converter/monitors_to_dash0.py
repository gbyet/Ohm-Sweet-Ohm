#!/usr/bin/env python3
"""
Alert converter V2: Datadog monitors JSON  ->  Prometheus alerting rules for Dash0.

Output is a PrometheusRule (JSON) using Dash0's threshold extension:
  expr: <PromQL> > $__threshold
  annotations: dash0-threshold-critical / dash0-threshold-degraded

Deterministic and rule-based; reuses the dashboard parser's query rules and
Datadog's metric catalog (see datadog_to_perses.py).

Usage:
    python3 monitors_to_dash0.py <monitors.json> [-o <rules.json>]
                                 [--metrics datadog|otel] [--verify] [--push]

--verify checks every metric name against Dash0 (read-only).
--push uploads each rule as a Dash0 check rule (skips names that already exist).
Both read DASH0_API_URL, DASH0_AUTH_TOKEN and DASH0_DATASET from the environment.

<monitors.json> is a list of monitors as returned by GET /api/v1/monitor, or one monitor,
or a Datadog asset file that wraps the monitor in {"definition": {...}}.
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import datadog_to_perses as dd

# aggregator(window):EXPRESSION comparator threshold
MONITOR = re.compile(
    r"^\s*(\w+)\(((?:last|current)_(\d+)([smhd]))\):(.*?)\s*(>=|<=|>|<|==|!=)\s*(-?[\d.]+)\s*$", re.S
)
# change(agg(last_X),last_Y):EXPRESSION comparator threshold  (also pct_change)
CHANGE = re.compile(
    r"^\s*(change|pct_change)\((\w+)\(last_(\d+)([smhd])\)\s*,\s*last_(\d+)([smhd])\):(.*?)"
    r"\s*(>=|<=|>|<|==|!=)\s*(-?[\d.]+)\s*$", re.S
)
LOG_MONITOR = re.compile(
    r'^\s*logs\("([^"]*)"\)\.index\("[^"]*"\)\.rollup\("count"\)(?:\.by\("([^"]*)"\))?'
    r'\.last\("(\d+[smhd])"\)\s*(>=|<=|>|<)\s*(-?[\d.]+)\s*$'
)
# a metric query without a space aggregator ("metric{...}"): Datadog's default is avg
BARE_METRIC = re.compile(r"(?<![\w:.])([a-zA-Z_][\w.]*)\{")
METRIC_QUERY = dd.METRIC_QUERY

# Datadog time aggregation of the monitor window -> Datadog widget aggregator
TIME_AGGREGATORS = {"sum": "sum", "avg": "avg", "min": "min", "max": "max", "last": "last"}


def number(x):
    """20.0 -> "20", 0.5 -> "0.5" (annotation values are strings)."""
    return str(int(x)) if float(x).is_integer() else str(x)


class Shifted:
    """The same translator, every query reference shifted back in time (change()/pct_change())."""

    def __init__(self, translator, shift):
        self.translator, self.shift = translator, shift

    def __call__(self, ref, shift=None):
        return self.translator(ref, shift or self.shift)

    @property
    def notes(self):
        return self.translator.notes

    @property
    def grouped(self):
        return self.translator.grouped


def build_expression(expr, time_agg, window, notes, shift=None):
    """Datadog monitor expression (metric queries + arithmetic) -> PromQL over the monitor window."""
    if time_agg not in TIME_AGGREGATORS:
        raise dd.Unsupported(f"monitor time aggregation '{time_agg}' has no rule")
    if BARE_METRIC.search(expr):
        expr = BARE_METRIC.sub(r"avg:\1{", expr)
        notes.append("no space aggregator in the monitor query: Datadog's default avg")

    # pull out each metric query -> query1, query2, ... and keep the arithmetic as a formula
    queries = []

    def replace(match):
        queries.append({"name": f"query{len(queries) + 1}", "data_source": "metrics",
                        "query": match.group(0), "aggregator": TIME_AGGREGATORS[time_agg]})
        return queries[-1]["name"]

    formula = METRIC_QUERY.sub(replace, expr)
    if not queries:
        raise dd.Unsupported(f"no metric query found in: {expr}")

    # a monitor evaluates one number over its window, like a single-number widget
    translator = dd.QueryTranslator(queries, "query_value", {}, notes)
    promql = dd.FormulaParser(formula, Shifted(translator, shift) if shift else translator).parse()
    promql = promql.replace("$__range", window).replace("$__interval", window)
    if "$__" in promql:
        raise dd.Unsupported(f"window variable left in expression: {promql}")
    return promql, translator


def metric_expression(monitor, notes):
    query = monitor["query"]
    c = CHANGE.match(query)
    if c:
        kind, time_agg, n, unit, shift_n, shift_unit, expr, comparator, _ = c.groups()
        window, shift = f"{n}{unit}", f"{shift_n}{shift_unit}"
        now, translator = build_expression(expr, time_agg, window, notes)
        before, _ = build_expression(expr, time_agg, window, notes, shift)
        if comparator in ("==", "!="):
            raise dd.Unsupported(f"comparator '{comparator}' has no rule (Dash0 thresholds are one-sided)")
        if kind == "change":
            return f"({now}) - ({before})", comparator, translator.metrics
        return f"(({now}) - ({before})) / ({before}) * 100", comparator, translator.metrics

    m = MONITOR.match(query)
    if not m:
        raise dd.Unsupported(f"monitor query shape not supported: {query}")
    time_agg, _, n, unit, expr, comparator, _ = m.groups()
    if comparator in ("==", "!="):
        raise dd.Unsupported(f"comparator '{comparator}' has no rule (Dash0 thresholds are one-sided)")
    promql, translator = build_expression(expr, time_agg, f"{n}{unit}", notes)
    return promql, comparator, translator.metrics


def log_expression(monitor, notes):
    m = LOG_MONITOR.match(monitor["query"])
    if not m:
        raise dd.Unsupported(f"log monitor shape not supported: {monitor['query']}")
    search, by, window, comparator, _ = m.groups()
    q = {"compute": {"aggregation": "count"}, "search": {"query": search},
         "group_by": [{"facet": f.strip()} for f in by.split(",")] if by else []}
    promql, _ = dd.log_count_query(q, True, {}, notes)
    return promql.replace("$__range", window), comparator, {"dash0.logs"}


SELECTOR = re.compile(r"\{[^{}]*\}")
SERVICE = re.compile(r'(?<![\w.])service_name="([^"]*)"')
AGGREGATION = re.compile(r"\b(sum|avg|min|max|count)(?: by \(([^)]*)\))? \(")


KEEP_BLOCKS = {"is_alert", "is_warning"}  # shown while the check fails
KEEP_INVERTED = {"is_recovery", "is_alert_recovery", "is_warning_recovery", "is_no_data"}
BLOCK = re.compile(r"\{\{([#^])(\w+)[^}]*\}\}(.*?)\{\{/\2\}\}", re.S)
VARIABLE = re.compile(r"\{\{\{?\s*([^{}]*?)\s*\}?\}\}")
TAG_VARIABLE = re.compile(r"^\[?([\w.-]+?)\]?(?:\.name)?$")
# Datadog's own variables (not tags): no Dash0 equivalent
DATADOG_VARIABLES = ("log.", "span.", "trace.", "event.", "org.", "eval", "first_triggered", "last_triggered",
                     "triggered_", "alert_", "ok_threshold", "warn_ok_threshold", "comparator", "link",
                     "check_message", "is_")
MENTION = re.compile(r"(?<![\w.])@[\w.+-]+(?:@[\w.-]+)?")


def message_template(text, notes):
    """Datadog message (Mustache-like) -> Dash0 description (Go template)."""
    def block(m):
        kind, name, body = m.groups()
        if (kind == "#" and name in KEEP_BLOCKS) or (kind == "^" and name in KEEP_INVERTED):
            return body
        notes.append(f"message part '{{{{{kind}{name}}}}}' dropped (Dash0 has one description)")
        return ""

    previous = None
    while previous != text:  # nested blocks: innermost first
        previous, text = text, BLOCK.sub(block, text)

    def variable(m):
        v = m.group(1)
        if v == "value":
            return "{{ $value }}"
        if v in ("threshold", "warn_threshold"):
            return "{{ $__threshold }}"
        t = TAG_VARIABLE.match(v)
        if t and not v.startswith(DATADOG_VARIABLES):
            return "{{ $labels.%s }}" % dd.tag_label(t.group(1), [])
        notes.append(f"message variable '{{{{{v}}}}}' has no Dash0 equivalent, removed")
        return ""

    text = VARIABLE.sub(variable, text)

    def mention(m):
        notes.append(f"notification '{m.group(0)}' not migrated: link a Dash0 notification channel")
        return ""

    text = MENTION.sub(mention, text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def keep_service(expr):
    """Group by service_name when every selector pins the same single service.

    The result is still one series, as in Datadog, but it keeps the service_name label,
    so Dash0 links the failed check to the service (service map, like Dash0's own rules)."""
    selectors = [s for s in SELECTOR.findall(expr) if "=" in s]
    services = {tuple(SERVICE.findall(s)) for s in selectors}
    if not selectors or len(services) != 1 or len(next(iter(services))) != 1:
        return expr

    def group(m):
        by = [b.strip() for b in (m.group(2) or "").split(",") if b.strip()]
        if "service_name" not in by:
            by.append("service_name")
        return f"{m.group(1)} by ({', '.join(by)}) ("

    return AGGREGATION.sub(group, expr)


def convert_monitor(monitor, report):
    name = monitor["name"]
    notes = []
    try:
        if monitor["type"] in ("query alert", "metric alert"):
            expr, comparator, metrics = metric_expression(monitor, notes)
        elif monitor["type"] == "log alert":
            expr, comparator, metrics = log_expression(monitor, notes)
        else:
            raise dd.Unsupported(f"monitor type '{monitor['type']}' has no rule")
    except dd.Unsupported as e:
        report.add("FAIL", name, [str(e)])
        return None

    thresholds = monitor.get("options", {}).get("thresholds", {})
    annotations = {"summary": message_template(name, notes),
                   "description": message_template(monitor.get("message", ""), notes)}
    if "critical" in thresholds:
        annotations["dash0-threshold-critical"] = number(thresholds["critical"])
    if "warning" in thresholds:
        annotations["dash0-threshold-degraded"] = number(thresholds["warning"])
    if "critical_recovery" in thresholds or "warning_recovery" in thresholds:
        notes.append("recovery thresholds dropped (Dash0 has no separate recovery level)")

    labels = {}
    for tag in monitor.get("tags", []):
        key, _, value = tag.partition(":")
        labels[re.sub(r"[^a-zA-Z0-9_]", "_", key)] = value or "true"

    report.add("WARN" if notes else "OK", name, notes, metrics)
    return {
        "alert": name,
        "expr": f"{keep_service(expr)} {comparator} $__threshold",
        "for": "0s",
        "labels": labels,
        "annotations": annotations,
    }


def unwrap(monitor):
    """Datadog's integration asset files wrap the monitor: {"version", "title", ..., "definition": {monitor}}."""
    if isinstance(monitor, dict) and "definition" in monitor and "query" not in monitor:
        return monitor["definition"]
    return monitor


def convert(monitors):
    if isinstance(monitors, dict):
        monitors = [monitors]
    monitors = [unwrap(m) for m in monitors]
    report = dd.Report()
    rules = [r for r in (convert_monitor(m, report) for m in monitors) if r]
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {"name": "migrated-datadog-monitors"},
        "spec": {"groups": [{"name": "Datadog", "interval": "1m", "rules": rules}]},
    }, report


def to_check_rule(rule, interval):
    """PrometheusRule entry -> Dash0 check rule API body."""
    a = rule["annotations"]
    thresholds = {}
    if "dash0-threshold-degraded" in a:
        thresholds["degraded"] = float(a["dash0-threshold-degraded"])
    if "dash0-threshold-critical" in a:
        thresholds["failed"] = float(a["dash0-threshold-critical"])
    return {
        "name": rule["alert"],
        "expression": rule["expr"],
        "thresholds": thresholds,
        "interval": interval,
        "for": rule["for"],
        "enabled": True,
        "summary": a.get("summary", ""),
        "description": a.get("description", ""),
        "labels": rule["labels"],
        "annotations": {},
    }


def push(rules_doc):
    base = os.environ.get("DASH0_API_URL", "https://api.europe-west4.gcp.dash0.com")
    token, dataset = os.environ["DASH0_AUTH_TOKEN"], os.environ.get("DASH0_DATASET", "default")
    url = f"{base}/api/alerting/check-rules?" + urllib.parse.urlencode({"dataset": dataset})
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        existing = {c.get("name") for c in json.load(r)}
    group = rules_doc["spec"]["groups"][0]
    for rule in group["rules"]:
        if rule["alert"] in existing:
            print(f"skip   {rule['alert']} (already in Dash0)")
            continue
        body = json.dumps(to_check_rule(rule, group["interval"])).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST")) as r:
            print(f"pushed {rule['alert']} (HTTP {r.status})")


def main():
    ap = argparse.ArgumentParser(description="Convert Datadog monitors to Prometheus alerting rules for Dash0.")
    ap.add_argument("input", help="Datadog monitors JSON (a list)")
    ap.add_argument("-o", "--output", help="PrometheusRule JSON file (default: stdout)")
    ap.add_argument("--metrics", choices=("datadog", "otel"), default="datadog",
                    help="how metrics arrive in Dash0: under their Datadog names (default) or as OpenTelemetry metrics")
    ap.add_argument("--verify", action="store_true", help="check every metric name against Dash0 (read-only)")
    ap.add_argument("--push", action="store_true", help="upload the rules to Dash0 as check rules")
    args = ap.parse_args()
    dd.OPTIONS["metrics"] = args.metrics

    with open(args.input, encoding="utf-8") as f:
        monitors = json.loads(f.read(), strict=False)

    rules, report = convert(monitors)
    if args.verify:
        known, dataset = dd.dash0_metric_names()
        print(f"Dash0 dataset '{dataset}' has data for {len(known)} metric names")
        report.verify(known, dataset)
    text = json.dumps(rules, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(text)

    report.print()
    if args.push:
        print()
        push(rules)
    sys.exit(1 if any(s == "FAIL" for s, _, _ in report.lines) else 0)


if __name__ == "__main__":
    main()
