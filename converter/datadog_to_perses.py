#!/usr/bin/env python3
"""
Forward parser V12: Datadog dashboard JSON  ->  Dash0 (Perses) dashboard JSON.

Deterministic and rule-based: no LLM, no randomness. Same input, same output.
The only network calls are the optional --verify and --push flags.

Usage:
    python3 datadog_to_perses.py <datadog.json | folder> [-o <perses.json | folder>]
                                 [--metrics datadog|otel] [--verify] [--push]

Metric knowledge comes from Datadog's own data, shipped next to this file as
datadog_metrics.csv.gz: the metric catalog of integrations-core (type, unit,
collection interval of every integration metric) and Datadog's published
OpenTelemetry metric mapping. Rules live in MIGRATION_NOTES.md.

--metrics datadog (default): metrics arrive in Dash0 under their Datadog names
    (Datadog Agent -> OpenTelemetry Collector with the Datadog receiver -> Dash0).
--metrics otel: metrics come from OpenTelemetry Collector receivers; Datadog's
    mapping table gives the OTel name, attribute filter and scale factor.
--verify: ask Dash0 which metric names have data and turn every "unverified"
    warning into OK (found) or FAIL (no data). Read-only.
--push: create/update the dashboard in Dash0 (PUT /api/dashboards/{origin}).
Both read DASH0_API_URL, DASH0_AUTH_TOKEN and DASH0_DATASET from the environment.
"""

import argparse
import csv
import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# Datadog widget type -> Perses panel kind (only kinds Dash0 can render)
WIDGET_TYPES = {
    "query_value": "StatChart",
    "timeseries": "TimeSeriesChart",
    "query_table": "TimeSeriesTable",
    "toplist": "TimeSeriesTable",  # Dash0 has no ranked bar list; a sorted table is closest
    "sunburst": "PieChart",
    "note": "Markdown",
    "free_text": "Markdown",
    "list_stream": "TimeSeriesChart",  # V6: log list -> log count chart (alternative)
    "image": "Markdown",  # V7: image -> Markdown ![](url)
    "heatmap": "TimeSeriesChart",  # V10: one line per series instead of colour bands
    "log_stream": "TimeSeriesChart",  # older log list widget -> log count chart, like list_stream
    "geomap": "TimeSeriesTable",  # map -> table, one row per region (single-level breakdown only)
}
SCALAR_WIDGETS = {"query_value", "query_table", "toplist", "sunburst", "geomap"}

# Datadog tag key -> PromQL label
TAGS = {
    "service": "service_name",
    "resource_name": "otel_span_name",
    "env": "deployment_environment_name",
    "host": "host_name",
    "http.status_code": "http_response_status_code",
}
UNVERIFIED_METRIC = "metric name unverified: assumes Dash0 receives it under the Datadog name"


def tag_label(key, notes):
    """Datadog tag -> PromQL label. Known tags from the table; any other tag passes through
    (leading '@' dropped, other characters -> '_', like OTel attributes in Prometheus)."""
    if key in TAGS:
        return TAGS[key]
    label = re.sub(r"[^a-zA-Z0-9_]", "_", key.lstrip("@"))
    if not label or label[0].isdigit():
        label = "_" + label
    notes.append(f"tag '{key}' passed through as label '{label}' (unverified)")
    return label


# Tags whose values Datadog rewrites (lowercase, spaces -> "_"); the original
# spelling is lost, so they are matched case-insensitively.
NORMALIZED_TAGS = {"resource_name"}

# Datadog trace operation -> span kind filter in Dash0
OPERATIONS = {
    "web.request": 'otel_span_kind="SERVER"',
    "glitchy_shop": None,  # internal spans of the shop (app-specific, reported as assumption)
}

# Datadog's OpenTelemetry operation names, read backwards (V12). Datadog names an OTel span by the
# first matching rule (docs.datadoghq.com/opentelemetry/migrate/migrate_operation_names):
#   http.request.method set        -> http.server.request / http.client.request
#   rpc.system set (and no method) -> <rpc.system>.server.request / <rpc.system>.client.request
# Only these two rules are used: they are the ones whose attributes Dash0's span metric carries.
OTEL_OPERATION = re.compile(r"([a-z][a-z0-9_]*)\.(server|client)\.request")
SPAN_KINDS = {"server": 'otel_span_kind="SERVER"', "client": 'otel_span_kind="CLIENT"'}


def otel_operation(operation, notes):
    """Datadog operation name of an OpenTelemetry span -> Dash0 span matchers, or None."""
    m = OTEL_OPERATION.fullmatch(operation)
    if not m:
        return None
    system, side = m.groups()
    if system == "http":
        notes.append(f"Datadog operation '{operation}': {side} spans with an HTTP method (Datadog's OTel naming rule)")
        return [SPAN_KINDS[side], 'http_request_method!=""']
    notes.append(f"Datadog operation '{operation}': {side} spans with rpc.system '{system}' (Datadog's OTel naming rule)")
    return [SPAN_KINDS[side], f'rpc_system="{system}"', 'http_request_method=""']


PERCENTILES = {"p50": "0.5", "p75": "0.75", "p90": "0.9", "p95": "0.95", "p99": "0.99"}
AGGREGATORS = {"sum", "avg", "min", "max", "count"}
TIME_SHIFTS = {"hour_before": "1h", "day_before": "1d", "week_before": "1w"}

# V6 alternatives for things PromQL can't do 1:1
ANOMALIES = re.compile(r"^\s*anomalies\(\s*(\w+)\s*,\s*'(\w+)'\s*,\s*([\d.]+)\s*(?:,.*)?\)\s*$", re.S)
LOG_SEVERITY = {"error": "ERROR", "warn": "WARN", "warning": "WARN", "info": "INFO", "debug": "DEBUG"}

# ---------------------------------------------------------------------------
# Datadog's metric catalog (V8)
#
# datadog_metrics.csv.gz, one row per Datadog metric:
#   metric, type (gauge|count|rate), unit, per_unit, interval,
#   otel_metric, otel_filter (k=v,...), otel_transform (*factor | rename:a=b)
# Built from DataDog/integrations-core */metadata.csv (+ the core "system" check)
# and Datadog's published OpenTelemetry metrics mapping (docs.datadoghq.com).
# ---------------------------------------------------------------------------

CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datadog_metrics.csv.gz")
OPTIONS = {"metrics": "datadog"}  # "datadog": names unchanged (Collector Datadog receiver); "otel": mapping table


def load_catalog(path=CATALOG_FILE):
    catalog = {}
    if not os.path.exists(path):
        return catalog
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            catalog[r["metric"]] = {
                "type": r["type"], "unit": r["unit"], "per_unit": r["per_unit"], "interval": r["interval"],
                "otel": r["otel_metric"], "otel_filter": r["otel_filter"], "otel_transform": r["otel_transform"],
            }
    return catalog


CATALOG = load_catalog()

# Datadog catalog unit -> Perses format unit (only exact equivalents; anything else gets no unit)
UNITS = {
    "byte": "bytes", "bit": "bits", "percent": "percent", "fraction": "percent-decimal",
    "nanosecond": "nanoseconds", "microsecond": "microseconds", "millisecond": "milliseconds",
    "second": "seconds", "minute": "minutes", "hour": "hours", "day": "days", "week": "weeks",
}
PER_SECOND_UNITS = {
    "byte": "bytes/sec", "bit": "bits/sec", "request": "requests/sec", "packet": "packets/sec",
    "message": "messages/sec", "operation": "ops/sec", "event": "events/sec", "record": "records/sec",
    "row": "rows/sec", "read": "reads/sec", "write": "writes/sec",
}
COUNTABLE_UNITS = {"query", "connection", "command", "error", "transaction", "document", "item",
                   "occurrence", "task", "job", "unit", "response", "lock", "call", "session"}


def perses_unit(unit, per_unit):
    if per_unit == "second":
        return PER_SECOND_UNITS.get(unit) or ("counts/sec" if unit in COUNTABLE_UNITS else None)
    if per_unit:
        return None
    return UNITS.get(unit)

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def is_name_note(message):
    """A note that only says 'this metric name is not verified in Dash0' (cleared by --verify)."""
    return message.startswith("metric name unverified") or "(unverified in Dash0)" in message


class Report:
    def __init__(self):
        self.lines, self.metrics = [], []

    def add(self, status, item, messages=(), metrics=()):
        self.lines.append((status, item, list(dict.fromkeys(messages))))
        self.metrics.append(sorted(set(metrics)))

    def verify(self, known, dataset):
        """Dash0 told us which metric names have data: unverified names become OK or FAIL."""
        for i, (status, item, msgs) in enumerate(self.lines):
            names = self.metrics[i]
            if status == "FAIL" or not names:
                continue
            missing = [n for n in names if n not in known]
            if missing:
                self.lines[i] = ("FAIL", item, msgs + [f"no data in Dash0 dataset '{dataset}' for {', '.join(missing)}"])
            else:
                kept = [m for m in msgs if not is_name_note(m)]
                self.lines[i] = ("WARN" if kept else "OK", item, kept)

    def print(self):
        icons = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
        print("\nMigration report (Datadog -> Dash0)")
        print("-----------------------------------")
        for status, item, messages in self.lines:
            print(f"{icons[status]} {item}")
            for m in messages:
                print(f"     - {m}")
        counts = {s: sum(1 for l in self.lines if l[0] == s) for s in icons}
        print(f"\n{counts['OK']} ok, {counts['WARN']} with warnings, {counts['FAIL']} failed")


class Unsupported(Exception):
    """Raised when a construct has no rule. It is reported, never guessed."""


# ---------------------------------------------------------------------------
# Datadog metric query -> PromQL
#
#   agg:metric{filters} [by {tags}] [.as_rate() | .as_count()] [.rollup(fn, N)] [.fill(x)]
# ---------------------------------------------------------------------------

QUERY = re.compile(
    r"^\s*(\w+):([\w.]+)\{([^{}]*)\}\s*(?:by\s*\{([^{}]*)\})?\s*((?:\.\w+\([^()]*\))*)\s*$", re.S
)
# "metric{...}" without "agg:" in front (Datadog's default space aggregator is avg)
BARE_METRIC = re.compile(r"(?<![\w:.$])([a-zA-Z_][\w.]*)\{")
BARE_QUERY = re.compile(r"^\s*[a-zA-Z_][\w.]*\{")
MODIFIER = re.compile(r"\.(\w+)\(([^()]*)\)")


def parse_modifiers(text, notes):
    """'.as_count().rollup(max, 60).fill(null)' -> ('as_count', ('max', 60))."""
    as_mod, rollup = None, None
    for name, args in MODIFIER.findall(text or ""):
        args = [a.strip().strip("'\"") for a in args.split(",")] if args.strip() else []
        if name in ("as_rate", "as_count"):
            if as_mod:
                raise Unsupported(f"both .{as_mod}() and .{name}() have no rule")
            as_mod = name
        elif name == "rollup":
            if len(args) != 2 or args[0] not in ("avg", "sum", "min", "max", "count") or not args[1].isdigit():
                raise Unsupported(f".rollup({', '.join(args)}) has no rule")
            rollup = (args[0], int(args[1]))
        elif name == "weighted":
            notes.append(".weighted() dropped (Datadog's time-weighting for gauges whose tags change)")
        elif name == "fill":
            if args[:1] != ["null"]:
                notes.append(f".fill({', '.join(args)}) dropped (gaps stay empty in Dash0)")
        else:
            raise Unsupported(f"modifier .{name}() has no rule")
    return as_mod, rollup


def regex_value(value):
    """Datadog-normalized value -> case-insensitive PromQL regex for the original value."""
    out = []
    for ch in value:
        if ch == "*":
            out.append(".*")
        elif ch == "_":
            out.append("[ _]")  # Datadog turned spaces into "_"
        elif ch in ".+?()[]{}|^$\\":
            out.append(f"[{ch}]" if ch not in "^]\\" else "\\\\" + ch)
        else:
            out.append(ch)
    return "(?i)^" + "".join(out) + "$"


def map_filters(text, notes, variables):
    """Datadog filter string -> list of PromQL label matchers."""
    text = text.strip()
    if text in ("", "*"):
        return []
    # terms are separated by "," or " AND " outside parentheses (an OR group or an IN list)
    terms = split_top_level(re.sub(r"\s+AND\s+", ",", text))

    matchers = []
    for term in terms:
        if term == "*":
            continue

        var = re.fullmatch(r"(!?)\$([\w.\-]+)", term)
        if var:
            neg, name = var.groups()
            if name not in variables:
                raise Unsupported(f"template variable ${name} is not defined")
            matchers.append(f'{variables[name]}{"!~" if neg else "=~"}"{var_ref(name)}"')
            continue

        group = re.fullmatch(r"(NOT\s+|!)?\((.+)\)", term)
        if group:  # (k:a OR k:b) -> k=~"a|b"; NOT (...) -> k!~"a|b"
            neg = bool(group.group(1))
            parts = [p.strip() for p in re.split(r"\s+OR\s+", group.group(2))]
            keys = {p.split(":", 1)[0] for p in parts}
            if len(keys) != 1 or any(":" not in p for p in parts):
                raise Unsupported(f"OR group mixes tags: {term}")
            key = keys.pop()
            label = tag_label(key, notes)
            values = [p.split(":", 1)[1] for p in parts]
            op = "!~" if neg else "=~"
            if key in NORMALIZED_TAGS:
                notes.append(f"'{key}' values matched case-insensitively (Datadog lowercased them)")
                pattern = "|".join(regex_value(v)[len("(?i)^"):-1] for v in values)
                matchers.append(f'{label}{op}"(?i)^({pattern})$"')
            else:
                matchers.append(f'{label}{op}"{"|".join(glob_regex(v) for v in values)}"')
            continue

        in_list = re.fullmatch(r"([\w.\-]+)\s+(NOT\s+)?IN\s*\(([^()]*)\)", term)
        if in_list:  # k IN (a,b,c) -> k=~"a|b|c"
            key, neg, values = in_list.group(1), bool(in_list.group(2)), [v.strip() for v in in_list.group(3).split(",")]
            label = tag_label(key, notes)
            matchers.append(f'{label}{"!~" if neg else "=~"}"{"|".join(glob_regex(v) for v in values if v)}"')
            continue

        m = re.fullmatch(r"(!?)([\w.\-]+):(.+)", term)
        if not m:
            raise Unsupported(f"filter '{term}' not supported")
        neg, key, value = m.groups()
        label = tag_label(key, notes)

        if key in NORMALIZED_TAGS or "*" in value:
            if key in NORMALIZED_TAGS:
                notes.append(f"'{key}' values matched case-insensitively (Datadog lowercased them)")
            matchers.append(f'{label}{"!~" if neg else "=~"}"{regex_value(value)}"')
        else:
            matchers.append(f'{label}{"!=" if neg else "="}"{value}"')
    return matchers


def map_metric(metric, prefix, notes):
    """Returns (dash0 metric name, extra matchers, kind, catalog entry, transform).

    kind is "percentile", "span" (Dash0 span metrics) or "passthrough" (any other metric).
    transform is "" or "*<factor>" (Datadog's OTel mapping, e.g. system.cpu.user = utilization × 100).
    """
    if not metric.startswith("trace."):
        if prefix in PERCENTILES:  # pNN only exists on Datadog distributions -> OTel histogram in Dash0
            notes.append(f"{prefix} of '{metric}': assumes the Datadog distribution reaches Dash0 as a histogram (unverified)")
            return metric, [], "percentile", None, ""
        entry = CATALOG.get(metric)
        if OPTIONS["metrics"] == "otel" and entry and entry["otel"]:
            # Datadog's published mapping, read backwards: OTel name + attribute filter + scale
            extra = [f'{k}="{v}"' for k, v in (kv.split("=", 1) for kv in entry["otel_filter"].split(",") if kv)]
            transform = entry["otel_transform"] if entry["otel_transform"].startswith("*") else ""
            if entry["otel_transform"].startswith("rename:"):
                otel_attr, dd_tag = entry["otel_transform"][len("rename:"):].split("=", 1)
                notes.append(f"Datadog tag '{dd_tag}' is the OTel attribute '{otel_attr}'")
            notes.append(f"'{metric}' -> OTel '{entry['otel']}' from Datadog's mapping table (unverified in Dash0)")
            return entry["otel"], extra, "passthrough", entry, transform
        # generic rule: the metric keeps its Datadog name (a WARN until --verify finds it in Dash0)
        notes.append(UNVERIFIED_METRIC)
        if OPTIONS["metrics"] == "otel":
            notes.append(f"'{metric}' has no OTel equivalent in Datadog's mapping table; Datadog name kept")
        return metric, [], "passthrough", entry, ""
    if prefix in PERCENTILES:
        operation, kind = metric[len("trace."):], "duration"
    else:
        m = re.fullmatch(r"trace\.(.+)\.(hits|errors)(\.by_http_status)?", metric)
        if not m:
            raise Unsupported(f"metric '{metric}' not in table")
        operation, kind = m.group(1), m.group(2)

    if operation in OPERATIONS:
        span_kind = OPERATIONS[operation]
        if span_kind is None:
            notes.append(f"Datadog operation '{operation}' assumed to be the shop's internal spans")
        extra = [span_kind] if span_kind else []
    else:
        extra = otel_operation(operation, notes)
        if extra is None:
            raise Unsupported(f"metric '{metric}' not in table")
    if kind == "duration":
        return "dash0.spans.duration", extra, "percentile", None, ""
    if kind == "errors":
        extra.append('otel_span_status_code="ERROR"')
    return "dash0.spans", extra, "span", None, ""


def translate_query(dd_query, aggregator, widget_type, variables, notes, shift=None):
    """Datadog metric query -> (promql, grouped, info), info = {"metric": Dash0 name, "unit": Perses unit|None}.

    The metric's Datadog type decides the PromQL shape (Datadog's catalog; see type_modifiers docs):
      gauge          value as it is; .as_rate()/.as_count() have no effect on a gauge
      count          per interval: .as_count() -> increase(), .as_rate() -> rate(),
                     no modifier -> Datadog shows the average count per collection interval -> rate() × interval
      rate           per second: no modifier/.as_rate() -> rate(), .as_count() -> increase() (× interval)
      unknown        gauge, unless a modifier says it is a counter
    Counters are assumed to reach Dash0 as cumulative sums (OpenTelemetry Collector, Datadog receiver).
    """
    if BARE_QUERY.match(dd_query):
        dd_query = "avg:" + dd_query.lstrip()
        notes.append("no space aggregator in the query: Datadog's default avg")
    m = QUERY.match(dd_query)
    if not m:
        raise Unsupported(f"query shape not supported: {dd_query}")
    prefix, metric, filters, by, modifier_text = m.groups()
    as_mod, rollup = parse_modifiers(modifier_text, notes)

    name, extra, metric_kind, entry, transform = map_metric(metric, prefix, notes)
    matchers = [f'otel_metric_name="{name}"'] + extra + map_filters(filters, notes, variables)
    selector = "{" + ", ".join(matchers) + "}"

    group = [tag_label(t.strip(), notes) for t in by.split(",")] if by else []
    by_clause = f" by ({', '.join(group)})" if group else ""

    scalar = widget_type in SCALAR_WIDGETS
    offset = f" offset {shift}" if shift else ""
    unit, per_unit = (entry["unit"], entry["per_unit"]) if entry else ("", "")

    def done(expr, unit=None, per_unit=None):
        if transform:
            expr = f"({expr}) {transform[0]} {transform[1:]}"
        return expr, bool(group), {"metric": name, "unit": perses_unit(unit, per_unit) if unit else None}

    if metric_kind == "percentile":
        if as_mod or rollup:
            raise Unsupported(f"modifier '{modifier_text.strip()}' on a percentile has no rule")
        window = "$__range" if scalar else "$__rate_interval"
        if scalar:
            notes.append(
                "percentile as a single number: Dash0 computes it over the whole window, "
                "Datadog averaged it per time slot (numbers can differ a lot)"
            )
        q = PERCENTILES[prefix]
        return done(f"histogram_quantile({q}, sum{by_clause} (rate({selector}[{window}]{offset})))")

    if prefix not in AGGREGATORS:
        raise Unsupported(f"aggregation '{prefix}' has no rule")
    bucket = f"{rollup[1]}s" if rollup else None  # single-number widgets: the rollup is the bucket size

    mtype = entry["type"] if entry else ""
    if metric_kind == "span":
        if as_mod is None:
            raise Unsupported("span metric without .as_rate()/.as_count() has no rule")
        counter = True
    elif mtype == "gauge":
        if as_mod:
            notes.append(f".{as_mod}() has no effect on a gauge (Datadog ignores it)")
        counter = False
    elif mtype in ("count", "rate"):
        counter = True
    else:
        counter = as_mod is not None
        if not counter:
            notes.append("no .as_rate()/.as_count() and metric not in Datadog's catalog: treated as a gauge")

    if not counter:
        inner = f"{rollup[0]}_over_time({selector}[{bucket}]{offset})" if rollup else f"{selector}{offset}"
        if not scalar or aggregator == "last":
            return done(f"{prefix}{by_clause} ({inner})", unit, per_unit)
        over_time = aggregator or "avg"
        if over_time not in ("avg", "sum", "min", "max"):
            raise Unsupported(f"gauge with aggregator '{aggregator}' has no rule")
        if rollup:  # aggregate the rollup buckets over the window (PromQL subquery)
            return done(f"{over_time}_over_time(({prefix}{by_clause} ({inner}))[$__range:{bucket}])", unit, per_unit)
        return done(f"{prefix}{by_clause} ({over_time}_over_time({selector}[$__range]{offset}))", unit, per_unit)

    # counters
    per_second, scale = as_mod == "as_rate" or (as_mod is None and mtype == "rate"), ""
    if as_mod is None and mtype == "count":
        interval = entry["interval"] or "15"
        notes.append(f"count without .as_count(): shown per {interval}s collection interval (Datadog's default view)")
        per_second, scale = True, f" * {interval}"
    window = bucket
    if rollup and rollup[0] not in ("sum", "avg"):
        notes.append(f".rollup({rollup[0]}, {rollup[1]}) on a count: Dash0 sums the {rollup[1]}s bucket")
    if per_second:
        if scalar:
            window = "$__range"
        elif not window:
            window = "$__rate_interval"
            notes.append("line shape: Dash0 smooths rate() over a window, Datadog computed each time slot")
        return done(f"{prefix}{by_clause} (rate({selector}[{window}]{offset})){scale}", unit, per_unit if scale else "second")
    if not scalar:
        return done(f"{prefix}{by_clause} (increase({selector}[{window or '$__interval'}]{offset}))", unit, "")
    agg = aggregator or "avg"
    if agg == "sum":
        return done(f"{prefix}{by_clause} (increase({selector}[$__range]{offset}))", unit, "")
    if agg == "last":
        return done(f"{prefix}{by_clause} (increase({selector}[{bucket or '$__interval'}]{offset}))", unit, "")
    if agg in ("avg", "min", "max"):
        bucket = bucket or "1m"
        if not rollup:
            notes.append(f"{agg} of counts: {agg} of the 1-minute counts in the window (Datadog's bucket size follows the time range)")
        return done(f"{agg}_over_time(({prefix}{by_clause} (increase({selector}[{bucket}]{offset})))[$__range:{bucket}])", unit, "")
    raise Unsupported(f".as_count() with aggregator '{aggregator}' has no rule")


# ---------------------------------------------------------------------------
# Datadog formula -> PromQL
#
#   expr := term (('+'|'-') term)* ; term := factor (('*'|'/') factor)*
#   factor := number | queryN | '(' expr ')' | func '(' args ')'
# ---------------------------------------------------------------------------


NUMBER = re.compile(r"-?\d+(\.\d+)?")


class FormulaParser:
    def __init__(self, text, translate):
        self.s, self.i, self.translate = text, 0, translate

    def peek(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1
        return self.s[self.i] if self.i < len(self.s) else ""

    def eat(self, ch):
        if self.peek() != ch:
            raise Unsupported(f"expected '{ch}' in formula '{self.s}'")
        self.i += 1

    def parse(self):
        out = self.expr()
        if self.peek():
            raise Unsupported(f"unexpected text in formula: '{self.s[self.i:]}'")
        return out

    def expr(self):
        out = self.term()
        while self.peek() in ("+", "-"):
            op = self.s[self.i]
            self.i += 1
            out += f" {op} {self.term()}"
        return out

    def term(self):
        out = self.factor()
        while self.peek() in ("*", "/"):
            op = self.s[self.i]
            self.i += 1
            out += f" {op} {self.factor()}"
        return out

    def number(self):
        self.peek()
        m = NUMBER.match(self.s, self.i)
        if not m:
            raise Unsupported(f"expected a number in formula '{self.s}'")
        self.i = m.end()
        return m.group()

    def string(self):
        self.peek()
        m = re.compile(r"'([^']*)'|\"([^\"]*)\"").match(self.s, self.i)
        if not m:
            raise Unsupported(f"expected a quoted argument in formula '{self.s}'")
        self.i = m.end()
        return m.group(1) if m.group(1) is not None else m.group(2)

    def factor(self, shift=None):
        if self.peek() == "-":
            self.i += 1
            return f"-{self.factor(shift)}"
        num = NUMBER.match(self.s, self.i)
        if num:
            self.i = num.end()
            return num.group()
        if self.peek() == "(":
            self.i += 1
            inner = self.expr()
            self.eat(")")
            return f"({inner})"

        name = re.compile(r"[a-zA-Z_]\w*").match(self.s, self.i)
        if not name:
            raise Unsupported(f"unexpected text in formula: '{self.s[self.i:]}'")
        word = name.group()
        self.i = name.end()

        if self.peek() != "(":  # a query reference like query1
            return self.translate(word, shift)

        self.eat("(")
        notes = self.translate.notes
        if word == "default_zero":
            inner, grouped = self.reference_or_expr(shift)
            self.eat(")")
            if grouped:
                notes.append("default_zero on a grouped query dropped (groups without data stay empty)")
                return inner
            return f"({inner} or vector(0))"
        if word in TIME_SHIFTS:
            inner, _ = self.reference_or_expr(TIME_SHIFTS[word])
            self.eat(")")
            return inner
        if word == "timeshift":
            ref = re.compile(r"\s*(\w+)\s*,\s*-(\d+)\s*").match(self.s, self.i)
            if not ref:
                raise Unsupported("timeshift() form not supported")
            self.i = ref.end()
            self.eat(")")
            return self.translate(ref.group(1), f"{ref.group(2)}s")
        if word in ("abs", "log2", "log10"):
            inner = self.expr()
            self.eat(")")
            return f"{word}({inner})"
        if word in ("count_nonzero", "count_not_null"):
            inner = self.expr()
            self.eat(")")
            return f"count(({inner}) != 0)" if word == "count_nonzero" else f"count({inner})"
        if word in ("clamp_min", "clamp_max", "cutoff_min", "cutoff_max", "max", "min"):
            a = self.expr()
            self.eat(",")
            b = self.expr()
            self.eat(")")
            if NUMBER.fullmatch(a) and not NUMBER.fullmatch(b):  # max(0, query1) == max(query1, 0)
                a, b = b, a
            if not NUMBER.fullmatch(b):
                raise Unsupported(f"{word}() of two series has no PromQL equivalent")
            inner, n = a, b
            if word in ("clamp_min", "max"):  # max(x, N): never below N
                return f"clamp_min({inner}, {n})"
            if word in ("clamp_max", "min"):
                return f"clamp_max({inner}, {n})"
            return f"(({inner}) {'>=' if word == 'cutoff_min' else '<='} {n})"  # cutoff: values past N dropped
        if word == "throughput":  # Datadog: each value divided by the bucket length in seconds
            inner = self.expr()
            self.eat(")")
            notes.append("throughput(): divided by the step in seconds (Datadog divided by its bucket length)")
            return f"(({inner}) / (${{__interval_ms}} / 1000))"
        if word == "rate":  # Datadog's rate() is the per-second rate of change, same as per_second()
            word = "per_second"
        if word in ("diff", "derivative", "monotonic_diff", "per_second", "per_minute", "per_hour"):
            inner = self.expr()
            self.eat(")")
            sub = f"(({inner})[$__interval:])"
            notes.append(f"{word}() computed over $__interval with a PromQL subquery (Datadog used consecutive points)")
            return {
                "diff": f"delta({sub})", "derivative": f"deriv({sub})", "monotonic_diff": f"increase({sub})",
                "per_second": f"rate({sub})", "per_minute": f"rate({sub}) * 60", "per_hour": f"rate({sub}) * 3600",
            }[word]
        if word == "exclude_null":
            inner = self.expr()
            if self.peek() == ",":
                self.i += 1
                self.string()
            self.eat(")")
            notes.append("exclude_null() dropped (PromQL has no null series)")
            return inner
        if word == "top":
            inner = self.expr()
            self.eat(",")
            n = self.number()
            fn, order = "mean", "desc"
            if self.peek() == ",":
                self.i += 1
                fn = self.string()
            if self.peek() == ",":
                self.i += 1
                order = self.string()
            self.eat(")")
            notes.append(f"top(): Dash0 ranks at each time step, Datadog ranked by '{fn}' over the whole range")
            return f"{'bottomk' if order == 'asc' else 'topk'}({n}, {inner})"
        if word == "rollup":
            inner = self.expr()
            self.eat(",")
            fn = self.string()
            self.eat(",")
            n = self.number()
            self.eat(")")
            if fn not in ("avg", "sum", "min", "max", "count"):
                raise Unsupported(f"rollup('{fn}') has no rule")
            notes.append(f"rollup('{fn}', {n}): Dash0 subquery over {n}s, Datadog used aligned {n}s buckets")
            return f"{fn}_over_time(({inner})[{n}s:])"
        raise Unsupported(f"formula function '{word}()' has no PromQL equivalent")

    def reference_or_expr(self, shift):
        """Inside default_zero()/hour_before(): a single query reference (possibly shifted)."""
        self.peek()
        name = re.compile(r"(query\w*|\w+)\s*(?=\))").match(self.s, self.i)
        if name:
            self.i = name.end()
            text = self.translate(name.group(1), shift)
            return text, self.translate.grouped.get(name.group(1), False)
        return self.expr(), False


class QueryTranslator:
    """Translates query references inside one widget request (callable)."""

    def __init__(self, queries, widget_type, variables, notes):
        self.queries = {q["name"]: q for q in queries}
        self.widget_type, self.variables, self.notes = widget_type, variables, notes
        self.grouped, self.metrics, self.units = {}, set(), {}

    def __call__(self, ref, shift=None):
        q = self.queries.get(ref)
        if q is None:
            raise Unsupported(f"formula refers to unknown query '{ref}'")
        if q.get("data_source") == "logs":
            promql, grouped = log_count_query(
                q, self.widget_type in SCALAR_WIDGETS, self.variables, self.notes, shift)
            self.grouped[ref], self.units[ref] = grouped, None
            self.metrics.add("dash0.logs")
            return promql
        if q.get("data_source", "metrics") != "metrics":
            raise Unsupported(f"data source '{q.get('data_source')}' is not a metric query")
        promql, grouped, info = translate_query(
            q["query"], q.get("aggregator"), self.widget_type, self.variables, self.notes, shift
        )
        self.grouped[ref], self.units[ref] = grouped, info["unit"]
        self.metrics.add(info["metric"])
        return promql


# ---------------------------------------------------------------------------
# Widget -> panel
# ---------------------------------------------------------------------------


def prom_query(expr, name):
    return {
        "kind": "TimeSeriesQuery",
        "spec": {"plugin": {"kind": "PrometheusTimeSeriesQuery", "spec": {"query": expr, "seriesNameFormat": name}}},
    }


def group_label(promql):
    m = re.search(r"by \(([^)]*)\)", promql)
    return "{{" + m.group(1).split(",")[0].strip() + "}}" if m else None


# one metric query inside an old-format "q" string or a monitor query
METRIC_QUERY = re.compile(r"\w+:[\w.]+\{[^{}]*\}(?:\s*by\s*\{[^{}]*\})?(?:\.\w+\([^()]*\))*")


def split_top_level(text):
    """'a{x,y}, b' -> ['a{x,y}', 'b'] (commas inside {} or () are kept)."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def upgrade_request(req):
    """Old request format {"q": "avg:m{..}.as_count(), sum:n{..}"} -> {"queries": [...], "formulas": [...]}.

    Each metric query becomes queryN; what is around it (arithmetic, functions) stays as the formula.
    Comma-separated series become separate formulas. alias / metadata.alias_name / limit+order carry over.
    """
    if "q" not in req or "queries" in req:
        return req
    out = {k: v for k, v in req.items() if k not in ("q", "metadata", "alias", "limit", "order")}
    aliases = {BARE_METRIC.sub(r"avg:\1{", m.get("expression") or ""): m.get("alias_name")
               for m in req.get("metadata") or []}
    queries, formulas = [], []

    def sub(match):
        queries.append({"name": f"query{len(queries) + 1}", "data_source": "metrics",
                        "query": match.group(0), "aggregator": req.get("aggregator", "avg")})
        return queries[-1]["name"]

    q = BARE_METRIC.sub(r"avg:\1{", req["q"])  # no space aggregator: Datadog's default avg
    series = split_top_level(q)
    for text in series:
        formula = {"formula": METRIC_QUERY.sub(sub, text)}
        alias = aliases.get(text) or (req.get("alias") if len(series) == 1 else None)
        if alias:
            formula["alias"] = alias
        if req.get("limit") and len(series) == 1:
            formula["limit"] = {"count": req["limit"], "order": req.get("order", "desc")}
        formulas.append(formula)
    out["queries"], out["formulas"] = queries, formulas
    return out


def upgrade_widget(w):
    """Widget with old-format requests -> same widget with queries/formulas (input not changed)."""
    defn = w.get("definition", {})
    reqs = defn.get("requests")
    if not isinstance(reqs, list) or not any("q" in r and "queries" not in r for r in reqs):
        return w
    return dict(w, definition=dict(defn, requests=[upgrade_request(r) for r in reqs]))


def convert_widget(defn, variables, report):
    hostmap = defn.get("type") == "hostmap"
    if hostmap:  # host map -> table of the colour ("fill") query, one row per host
        fill = (defn.get("requests") or {}).get("fill") or {}
        if "q" not in fill:
            report.add("FAIL", defn.get("title") or "Host map", ["hostmap without a fill query"])
            return placeholder(defn.get("title") or "Host map", "Datadog host map without a fill query was not migrated.")
        defn = dict(defn, type="query_table", title=defn.get("title") or "Host map",
                    requests=[upgrade_request({"q": fill["q"], "aggregator": "avg"})])
    wtype = defn.get("type")
    title = defn.get("title") or {"note": "Note", "free_text": "Text", "image": "Image", "log_stream": "Logs", "geomap": "Map"}.get(wtype, wtype)
    kind = WIDGET_TYPES.get(wtype)

    if kind is None:
        report.add("FAIL", title, [f"widget type '{wtype}' has no Dash0 equivalent"])
        return placeholder(title, f"Datadog widget type '{wtype}' was not migrated.")

    if wtype == "image":
        if not defn.get("url"):
            report.add("FAIL", title, ["image widget without a url"])
            return placeholder(title, "Datadog image widget without a url was not migrated.")
        report.add("OK", title)
        return panel(title, "Markdown", {"text": f"![]({defn['url']})"}, [])

    if kind == "Markdown":
        report.add("OK", title)
        text = defn.get("content") or defn.get("text", "")
        return panel(title, "Markdown", {"text": text}, [])

    notes, queries, metrics, units = [], [], set(), []
    if wtype in ("list_stream", "log_stream"):
        try:
            queries = convert_log_stream(defn, notes, variables)
        except Unsupported as e:
            report.add("FAIL", title, [str(e)])
            return placeholder(title, f"Not migrated: {e}")
        report.add("WARN", title, notes, ["dash0.logs"])
        return panel(title, "TimeSeriesChart", {"visual": {"display": "bar"}}, queries)

    try:
        for req in defn.get("requests", []):
            if "queries" in req and "formulas" not in req:
                # formulas are optional in Datadog: without them every query is shown as it is
                req = dict(req, formulas=[{"formula": q["name"]} for q in req["queries"] if "name" in q])
            if "queries" not in req or "formulas" not in req:
                raise Unsupported("request without queries/formulas (old Datadog format) has no rule")
            translator = QueryTranslator(req["queries"], wtype, variables, notes)
            limited = len(req["formulas"]) == 1
            for f in req["formulas"]:
                anomaly = ANOMALIES.match(f["formula"])
                if anomaly:
                    queries.extend(anomaly_band(anomaly, translator, f.get("alias") or title))
                    units.append(None)
                    continue
                promql = FormulaParser(f["formula"], translator).parse()
                ref = re.fullmatch(r"\s*(\w+)\s*", f["formula"])  # a bare query reference keeps its unit
                units.append(translator.units.get(ref.group(1)) if ref else None)
                limit = f.get("limit")
                if limit and "count" not in limit:
                    notes.append("row limit without a count dropped")
                elif limit and limited:
                    k = "topk" if limit.get("order", "desc") == "desc" else "bottomk"
                    promql = f"{k}({limit['count']}, {promql})"
                elif limit:
                    notes.append("row limit dropped (table has several columns)")
                if wtype in ("query_table", "toplist", "geomap"):
                    # rows are named after the group (e.g. the route), not the column
                    name = group_label(promql) or f.get("alias") or title
                else:
                    name = f.get("alias") or group_label(promql) or title
                queries.append(prom_query(promql, name))
            metrics |= translator.metrics
    except Unsupported as e:
        report.add("FAIL", title, [str(e)])
        return placeholder(title, f"Not migrated: {e}")

    spec = {}
    if kind in ("StatChart", "TimeSeriesTable", "PieChart"):
        spec["calculation"] = "last"
    # unit from Datadog's catalog, only when every query in the panel has the same one
    unit = units[0] if units and all(u == units[0] for u in units) else None
    if kind == "StatChart" and defn.get("custom_unit") == "%":
        spec["format"] = {"unit": "percent"}
    elif unit and kind == "StatChart":
        spec["format"] = {"unit": unit}
    elif unit and kind == "TimeSeriesChart":
        spec["yAxis"] = {"format": {"unit": unit}}
    if kind == "TimeSeriesChart":
        display = {r.get("display_type", "line") for r in defn.get("requests", [])}
        spec["visual"] = {"display": "bar" if display == {"bars"} else "line"}
    if wtype == "toplist":
        notes.append("toplist shown as a table (Dash0 has no ranked bar list)")
    if wtype == "geomap":
        levels = [group_level_count(q["spec"]["plugin"]["spec"]["query"]) for q in queries]
        if not levels or any(n != 1 for n in levels):
            why = "geomap is not a single-level breakdown (one group-by per query): a table would not show the same thing"
            report.add("FAIL", title, [why])
            return placeholder(title, f"Not migrated: {why}")
        notes.append("geomap shown as a table, one row per region (Dash0 has no map panel)")
    if hostmap:
        notes.append("host map shown as a table, one row per host (Dash0 has no host map); size query dropped")
    if wtype == "heatmap":
        notes.append("heatmap shown as a line chart, one line per series (Dash0 has no heatmap panel)")

    report.add("WARN" if notes else "OK", title, notes, metrics)
    return panel(title, kind, spec, queries)


def anomaly_band(match, translator, name):
    """anomalies(q, 'algo', N) -> the series plus a "normal range" band.

    Band = mean over the last hour +/- N standard deviations (PromQL subqueries).
    An approximation: Datadog's algorithms (basic/agile/robust) are not open.
    """
    ref, algorithm, bounds = match.groups()
    series = translator(ref)
    window = "[1h:1m]"
    mean = f"avg_over_time(({series}){window})"
    spread = f"stddev_over_time(({series}){window})"
    translator.notes.append(
        f"anomalies('{algorithm}', {bounds}) approximated: normal range = 1h mean ± {bounds}× std dev "
        "(Datadog's algorithm is not open)"
    )
    return [
        prom_query(series, name),
        prom_query(f"{mean} + {bounds} * {spread}", "normal range (upper)"),
        prom_query(f"clamp_min({mean} - {bounds} * {spread}, 0)", "normal range (lower)"),
    ]


LOG_TOKEN = re.compile(r'\s*(-?)((?:[@\w.\-]+:)?(?:"[^"]*"|\([^()]*\)|[^\s()"]+)|\S+)')
LOG_NOTE = "log count from Dash0's synthetic metric dash0.logs"


def glob_regex(value):
    """Datadog wildcard value -> PromQL regex ('*' -> '.*', the rest literal)."""
    out = []
    for ch in value:
        if ch == "*":
            out.append(".*")
        elif ch in "^]":
            out.append("\\\\" + ch)  # backslash-escaped (doubled inside the PromQL string)
        elif ch in ".+?()[{}|$":
            out.append(f"[{ch}]")
        else:
            out.append(ch)
    return "".join(out)


def var_ref(name):
    """Perses variable reference: $name, or ${name} when the name has '-' or '.'."""
    return f"${name}" if re.fullmatch(r"\w+", name) else "${" + name + "}"


def log_matchers(search, notes, variables=None):
    """Datadog log search -> PromQL matchers on dash0.logs.

    Supported terms (all ANDed): status:<level>, key:value (known tags from the table, any
    other tag or @attribute passes through as a label, WARN), key:"quoted", key:val*,
    key:(a OR b), -key:value, $variable. Anything else (free text, OR/NOT between terms,
    ranges, comparisons) has no rule and fails: the search string is never dropped.
    """
    variables = variables or {}
    matchers, pos, search = [], 0, search.strip()
    if search == "*":
        return []
    while pos < len(search):
        m = LOG_TOKEN.match(search, pos)
        if not m or not m.group(0).strip():
            break
        pos = m.end()
        neg, term = m.group(1) == "-", m.group(2)
        if term == "AND" and not neg:
            continue
        var = re.fullmatch(r"\$([\w.\-]+)", term)
        if var and not neg:
            if var.group(1) not in variables:
                raise Unsupported(f"template variable ${var.group(1)} is not defined")
            matchers.append(f'{variables[var.group(1)]}=~"{var_ref(var.group(1))}"')
            continue
        kv = re.fullmatch(r"([@\w.\-]+):(.+)", term)
        if not kv:
            raise Unsupported(f"log search term '{term}' has no rule")
        key, value = kv.groups()
        if key == "status" and not neg and value.lower() in LOG_SEVERITY:
            matchers.append(f'otel_log_severity_range="{LOG_SEVERITY[value.lower()]}"')
            continue
        if key == "status" or re.match(r"^[<>\[{]", value) or "\\" in value:
            raise Unsupported(f"log search term '{term}' has no rule")
        label = tag_label(key, notes)
        ref = re.fullmatch(r"\$([\w\-]+)(?:\.value)?", value)
        if ref:
            if ref.group(1) not in variables:
                raise Unsupported(f"template variable ${ref.group(1)} is not defined")
            matchers.append(f'{label}{"!~" if neg else "=~"}"{var_ref(ref.group(1))}"')
            continue
        group = re.fullmatch(r"\((.+)\)", value)
        if group:  # key:(a OR b OR c)
            values = [v.strip().strip('"') for v in re.split(r"\s+OR\s+", group.group(1).strip())]
            if any(not v or re.search(r"[\s()]", v) for v in values):
                raise Unsupported(f"log search term '{term}' has no rule")
            matchers.append(f'{label}{"!~" if neg else "=~"}"{"|".join(glob_regex(v) for v in values)}"')
            continue
        quoted = value.startswith('"')
        value = value.strip('"')
        if not value:
            raise Unsupported(f"log search term '{term}' has no rule")
        if "*" in value and not quoted:
            matchers.append(f'{label}{"!~" if neg else "=~"}"{glob_regex(value)}"')
        else:
            matchers.append(f'{label}{"!=" if neg else "="}"{value}"')
    return matchers


def log_count_query(q, scalar, variables, notes, shift=None):
    """Datadog "logs" data source query (count, optional group by facets) -> dash0.logs PromQL."""
    compute = q.get("compute") or {}
    if compute.get("aggregation") != "count" or compute.get("metric"):
        what = str(compute.get("aggregation")) + (f" of {compute['metric']}" if compute.get("metric") else "")
        raise Unsupported(f"log aggregation '{what}' has no rule (only count)")
    matchers = ['otel_metric_name="dash0.logs"'] + log_matchers(
        (q.get("search") or {}).get("query", ""), notes, variables)
    group_by = q.get("group_by") or []
    if isinstance(group_by, dict):  # newer form: {"fields": [...], "limit": N, "sort": {...}}
        facets, limited = group_by.get("fields") or [], bool(group_by.get("limit"))
    else:  # [{"facet": "@x", "limit": N, ...}]
        facets, limited = [g["facet"] for g in group_by], any(g.get("limit") for g in group_by)
    group = [tag_label(f, notes) for f in facets]
    if group and limited:
        notes.append("log group limit dropped (all groups shown)")
    notes.append(LOG_NOTE)
    by_clause = f" by ({', '.join(group)})" if group else ""
    window = "$__range" if scalar else "$__interval"
    offset = f" offset {shift}" if shift else ""
    selector = "{" + ", ".join(matchers) + "}"
    return f"sum{by_clause} (increase({selector}[{window}]{offset}))", bool(group)


def group_level_count(promql):
    """Number of labels in the outermost 'by (...)' of a PromQL query (0 when ungrouped)."""
    m = re.search(r"by \(([^)]*)\)", promql)
    return len([x for x in m.group(1).split(",") if x.strip()]) if m else 0


def convert_log_stream(defn, notes, variables=None):
    """Datadog log list -> count of matching logs over time (Dash0 synthetic metric dash0.logs).

    Dash0 dashboards have no log list panel; the matching log lines are in Dash0's Logs explorer.
    list_stream holds the search in requests[0].query.query_string, the older log_stream in "query".
    """
    if defn.get("type") == "log_stream":
        search = (defn.get("query") or "").strip()
        matchers = ['otel_metric_name="dash0.logs"'] + log_matchers(search, notes, variables)
        notes.append("log list shown as a log count chart (Dash0 dashboards have no log list; see Dash0 Logs)")
        selector = "{" + ", ".join(matchers) + "}"
        return [prom_query(f"sum(increase({selector}[$__interval]))", "matching logs")]
    reqs = defn.get("requests", [])
    source = (reqs[0].get("query", {}).get("data_source") if len(reqs) == 1 else None)
    if source and source != "logs_stream":
        raise Unsupported(f"list of '{source}' items has no Dash0 equivalent (only log lists are migrated)")
    if len(reqs) != 1 or reqs[0].get("query", {}).get("data_source") != "logs_stream":
        raise Unsupported("only a single logs_stream request is supported")
    search = reqs[0]["query"].get("query_string", "").strip()
    matchers = ['otel_metric_name="dash0.logs"'] + log_matchers(search, notes, variables)
    notes.append("log list shown as a log count chart (Dash0 dashboards have no log list; see Dash0 Logs)")
    selector = "{" + ", ".join(matchers) + "}"
    return [prom_query(f"sum(increase({selector}[$__interval]))", "matching logs")]


def panel(title, kind, spec, queries):
    return {"kind": "Panel", "spec": {"display": {"name": title}, "plugin": {"kind": kind, "spec": spec}, "queries": queries}}


def placeholder(title, text):
    return panel(title, "Markdown", {"text": f"**{title}**\n\n⚠️ {text}"}, [])


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "panel"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def convert_variables(template_vars, report):
    mapping, out = {}, []
    for tv in template_vars or []:
        name, prefix = tv["name"], tv.get("prefix") or tv["name"]
        notes = []
        label = tag_label(prefix, notes)
        mapping[name] = label
        # known tags: values from Dash0's span metric; passed-through tags: from any metric
        matchers = [] if notes else ['{otel_metric_name="dash0.spans"}']
        out.append({
            "kind": "ListVariable",
            "spec": {
                "name": name,
                "display": {"name": name},
                "allowAllValue": True,
                "allowMultiple": False,
                "defaultValue": "$__all",
                "plugin": {
                    "kind": "PrometheusLabelValuesVariable",
                    "spec": {"labelName": label, "matchers": matchers},
                },
            },
        })
        status = "WARN" if notes else "OK"  # only passed-through tags make it a WARN
        if tv.get("default", "*") != "*":
            notes.append("default value reset to 'all'")
        report.add(status, f"variable ${name}", notes)
    return mapping, out


def split_tables(widgets, report, grid_width=12):
    """A Datadog table with N columns (formulas) -> N single-column tables.

    Datadog lines up the columns by group (e.g. route). Dash0's table shows one row
    per series and does not join several queries into columns, so each column
    becomes its own table that keeps the route names.

    Layout: the N tables get their own full-width row (each 1/N of the dashboard),
    placed below everything that starts above the original table. Widgets that
    started at or below the original table move down by the table's height.
    """
    out = list(widgets)
    while True:
        idx = next(
            (i for i, w in enumerate(out)
             if w["definition"].get("type") == "query_table"
             and len(w["definition"].get("requests", [])) == 1
             and len(w["definition"]["requests"][0].get("formulas", [])) >= 2),
            None,
        )
        if idx is None:
            return out
        w = out.pop(idx)
        defn, layout = w["definition"], w.get("layout", {})
        req = defn["requests"][0]
        formulas = req["formulas"]
        top, height = layout.get("y", 0), layout.get("height", 4)

        # new row starts below every widget that begins above the original table
        row_y = max([o["layout"]["y"] + o["layout"]["height"] for o in out
                     if o.get("layout", {}).get("y", 0) < top] + [top])
        moved = [o for o in out if o.get("layout", {}).get("y", 0) >= top]
        if moved:
            shift = max(0, row_y + height - min(o["layout"]["y"] for o in moved))
            for o in moved:
                o["layout"] = dict(o["layout"], y=o["layout"]["y"] + shift)

        title, part, names = defn.get("title", "Table"), grid_width / len(formulas), []
        for i, f in enumerate(formulas):
            used = set(re.findall(r"\b(query\w*)\b", f["formula"]))
            column = f.get("alias") or f"column {i + 1}"
            names.append(column)
            new = dict(defn, title=f"{title} · {column}")
            new["requests"] = [dict(req, formulas=[f], queries=[q for q in req["queries"] if q["name"] in used])]
            out.insert(idx + i, {"definition": new,
                                 "layout": {"x": part * i, "y": row_y, "width": part, "height": height}})
        report.add("WARN", title, [
            f"table with {len(formulas)} columns split into {len(formulas)} tables on their own row: "
            + ", ".join(names)
        ])


def sections(widgets):
    """Top-level widgets -> [(title or None, widgets)].

    A Datadog group becomes its own titled section (its children's layout is already
    relative to the group). Widgets outside groups are collected into untitled sections,
    one per run between groups. Without groups there is one untitled section.
    """
    out = []
    for w in widgets:
        defn = w["definition"]
        if defn.get("type") == "group":
            out.append((defn.get("title") or "Group", defn.get("widgets", [])))
        elif out and out[-1][0] is None:
            out[-1][1].append(w)
        else:
            out.append((None, [w]))
    return out or [(None, [])]


def convert(dd):
    if "widgets" not in dd:
        raise SystemExit("Input is not a Datadog dashboard (no 'widgets').")
    report = Report()
    variables, perses_vars = convert_variables(dd.get("template_variables"), report)

    parts = sections(dd["widgets"])
    has_groups = any(t is not None for t, _ in parts)
    panels, layouts, used = {}, [], set()
    for section_title, widgets in parts:
        items = []
        widgets = split_tables([upgrade_widget(w) for w in widgets], report)
        # untitled run between groups: start at the top of its own section
        top = min((w.get("layout", {}).get("y", 0) for w in widgets), default=0) if has_groups else 0
        for w in widgets:
            defn = w["definition"]
            pid = slug(defn.get("title") or defn.get("type"))
            base, n = pid, 2
            while pid in used:
                pid, n = f"{base}-{n}", n + 1
            used.add(pid)

            panels[pid] = convert_widget(defn, variables, report)
            layout = w.get("layout", {})
            # Datadog grid: 12 columns. Perses grid: 24 columns.
            items.append({
                "content": {"$ref": f"#/spec/panels/{pid}"},
                "x": round(layout.get("x", 0) * 2),
                "y": round((layout.get("y", 0) - top) * 3),
                "width": round(layout.get("width", 6) * 2),
                "height": round(layout.get("height", 2) * 3),
            })
        spec = {"items": items}
        if section_title is not None:
            # Datadog group -> Perses collapsible panel group (open by default)
            spec = {"display": {"title": section_title, "collapse": {"open": True}}, "items": items}
        layouts.append({"kind": "Grid", "spec": spec})

    title = dd.get("title", "Migrated dashboard").replace("Datadog", "Dash0")
    return {
        "apiVersion": "perses.dev/v1alpha1",
        "kind": "PersesDashboard",
        "metadata": {"name": slug(title)},
        "spec": {
            "display": {"name": title, "description": dd.get("description") or ""},
            "duration": "1h",
            "variables": perses_vars,
            "layouts": layouts,
            "panels": panels,
        },
    }, report


# ---------------------------------------------------------------------------
# Dash0 API (only used by --verify / --push)
# ---------------------------------------------------------------------------


def dash0_request(path, method="GET", body=None):
    base = os.environ.get("DASH0_API_URL", "https://api.europe-west4.gcp.dash0.com").rstrip("/")
    token = os.environ.get("DASH0_AUTH_TOKEN")
    if not token:
        raise SystemExit("DASH0_AUTH_TOKEN is not set (Dash0: Settings -> Auth Tokens)")
    dataset = os.environ.get("DASH0_DATASET", "default")
    url = f"{base}{path}{'&' if '?' in path else '?'}" + urllib.parse.urlencode({"dataset": dataset})
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=60) as r:
        return json.loads(r.read().decode() or "null"), dataset


def dash0_metric_names():
    """Every metric name that has data in the dataset (Dash0's Prometheus API)."""
    data, dataset = dash0_request("/api/prometheus/api/v1/label/otel_metric_name/values")
    return set(data.get("data", [])), dataset


def push_dashboard(perses):
    """Create or update the dashboard in Dash0; the origin 'datadog-<slug>' makes a re-run an update."""
    name = perses["metadata"]["name"]
    body = {"kind": "Dashboard", "metadata": {"name": name}, "spec": perses["spec"]}
    data, dataset = dash0_request("/api/dashboards/" + urllib.parse.quote(f"datadog-{name}"), "PUT", body)
    return (data or {}).get("id", f"datadog-{name}"), dataset


def run(path, out_path, args, known=None):
    with open(path, encoding="utf-8") as f:
        dd = json.loads(f.read(), strict=False)

    perses, report = convert(dd)
    if known is not None:
        report.verify(*known)
    text = json.dumps(perses, indent=2, ensure_ascii=False) + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {out_path}")
    else:
        sys.stdout.write(text)

    report.print()
    if args.push:
        dashboard_id, dataset = push_dashboard(perses)
        print(f"pushed dashboard '{perses['spec']['display']['name']}' to Dash0 dataset '{dataset}' (id {dashboard_id})")
    return not any(s == "FAIL" for s, _, _ in report.lines)


def main():
    ap = argparse.ArgumentParser(description="Convert Datadog dashboards to Dash0 (Perses) dashboards.")
    ap.add_argument("input", help="Datadog dashboard JSON file, or a folder of them")
    ap.add_argument("-o", "--output", help="Perses dashboard JSON file (default: stdout); a folder for a folder input")
    ap.add_argument("--metrics", choices=("datadog", "otel"), default="datadog",
                    help="how metrics arrive in Dash0: under their Datadog names (default) or as OpenTelemetry metrics")
    ap.add_argument("--verify", action="store_true", help="check every metric name against Dash0 (read-only)")
    ap.add_argument("--push", action="store_true", help="create/update the dashboard in Dash0")
    args = ap.parse_args()
    OPTIONS["metrics"] = args.metrics

    known = None
    if args.verify:
        known = dash0_metric_names()
        print(f"Dash0 dataset '{known[1]}' has data for {len(known[0])} metric names")

    if os.path.isdir(args.input):
        if not args.output:
            ap.error("-o <folder> is required when the input is a folder")
        os.makedirs(args.output, exist_ok=True)
        ok = True
        for name in sorted(os.listdir(args.input)):
            if name.endswith(".json"):
                print(f"\n=== {name}")
                ok &= run(os.path.join(args.input, name), os.path.join(args.output, name), args, known)
        sys.exit(0 if ok else 1)
    sys.exit(0 if run(args.input, args.output, args, known) else 1)


if __name__ == "__main__":
    main()
