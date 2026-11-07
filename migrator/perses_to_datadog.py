#!/usr/bin/env python3
"""
Reverse parser v4: Dash0 (Perses) dashboard JSON  ->  Datadog dashboard JSON.

Deterministic and rule-based: no LLM, no randomness, no network.
Same input always gives the same output.

Usage:
    python3 perses_to_datadog.py <perses.json> [-o <datadog.json>]

Rules live in MIGRATION_NOTES.md (concepts, not yet proven).

v2 adds: arithmetic formulas (+ - * / numbers, brackets), `or vector(0)`,
histogram_quantile -> pNN percentiles, regex alternation (a|b|c) filters,
and internal (non-server) spans.

v3 adds: `sum by (...)` inside formulas (bug fix), `offset` -> timeshift(),
HTTP status breakdown, topk(k, ...) -> table limit, Table panels,
dashboard variables, and "Dash0" -> "Datadog" in the title.

v4 adds: Dash0's real table type (TimeSeriesTable), PieChart -> sunburst,
a check that every panel type is a real Dash0 type, a warning on rate charts
(different smoothing), and readable column names for tables.
"""

import argparse
import json
import re
import sys

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# Perses panel plugin kind -> Datadog widget type
PANEL_TYPES = {
    "StatChart": "query_value",
    "TimeSeriesChart": "timeseries",
    "Markdown": "note",
    "TimeSeriesTable": "query_table",
    "PieChart": "sunburst",
}

# Panel types Dash0 can actually render (from Dash0's chart type reference).
# Anything else is a broken input: Dash0 saves it but shows "unsupported widget type".
DASH0_PANEL_TYPES = {"Markdown", "StatChart", "TimeSeriesChart", "TimeSeriesTable", "PieChart"}

SCALAR_WIDGETS = {"query_value", "query_table", "sunburst"}

# PromQL label -> Datadog tag key
LABELS = {
    "service_name": "service",
    "otel_span_name": "resource_name",
    "deployment_environment_name": "env",
    "host_name": "host",
    "http_response_status_code": "http.status_code",
}

# Grouping by HTTP status needs Datadog's dedicated ".by_http_status" metric.
STATUS_LABEL = "http_response_status_code"
OFFSET_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Labels that only identify the metric; they are consumed by the metric mapping.
METRIC_LABELS = {"otel_metric_name", "otel_span_kind", "otel_span_status_code"}

# Datadog names trace metrics after the span's "operation":
#   server spans          -> web.request
#   internal/custom spans -> the instrumentation scope name (here: tracer "glitchy-shop")
# The second one is app-specific, so it is configured here and reported as an assumption.
SERVER_OPERATION = "web.request"
INTERNAL_OPERATION = "glitchy_shop"

AGGREGATORS = {"sum", "avg", "min", "max"}
QUANTILES = {"0.5": "p50", "0.75": "p75", "0.9": "p90", "0.95": "p95", "0.99": "p99"}

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class Report:
    """Collects one status per panel: OK, WARN (assumption made) or FAIL."""

    def __init__(self):
        self.lines = []

    def add(self, status, panel, messages=()):
        self.lines.append((status, panel, list(dict.fromkeys(messages))))

    def print(self):
        icons = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
        print("\nMigration report")
        print("----------------")
        for status, panel, messages in self.lines:
            print(f"{icons[status]} {panel}")
            for m in messages:
                print(f"     - {m}")
        counts = {s: sum(1 for l in self.lines if l[0] == s) for s in icons}
        print(f"\n{counts['OK']} ok, {counts['WARN']} with warnings, {counts['FAIL']} failed")


class Unsupported(Exception):
    """Raised when a construct has no rule. It is reported, never guessed."""


# ---------------------------------------------------------------------------
# Step 1: split a PromQL expression into arithmetic + "leaves"
#
#   expr   := term (('+' | '-') term)*
#   term   := factor (('*' | '/') factor)*
#   factor := number | '(' expr ')' | leaf      [optionally followed by "or vector(0)"]
#   leaf   := name '(' ... ')'                   (a full metric query, e.g. sum(rate(...)))
#
# Arithmetic is kept as a Datadog formula; each leaf becomes one Datadog query.
# ---------------------------------------------------------------------------

OR_ZERO = re.compile(r"\s*or\s+vector\s*\(\s*0\s*\)")


class ExprParser:
    def __init__(self, text):
        self.s, self.i = text, 0
        self.leaves = []  # PromQL text of each leaf, in order
        self.notes = []

    def skip(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def peek(self):
        self.skip()
        return self.s[self.i] if self.i < len(self.s) else ""

    def parse(self):
        formula = self.expr()
        self.skip()
        if self.i != len(self.s):
            raise Unsupported(f"unexpected text: '{self.s[self.i:]}'")
        return formula

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

    def factor(self):
        self.skip()
        number = re.compile(r"\d+(\.\d+)?").match(self.s, self.i)
        if number:
            self.i = number.end()
            return number.group()

        if self.peek() == "(":
            self.i += 1
            inner = self.expr()
            if self.peek() != ")":
                raise Unsupported("unbalanced brackets")
            self.i += 1
            out = f"({inner})"
        else:
            name = re.compile(r"[a-zA-Z_]\w*").match(self.s, self.i)
            if not name:
                raise Unsupported(f"unexpected text: '{self.s[self.i:]}'")
            pos = name.end()
            by = re.compile(r"\s*(by|without)\s*\([^)]*\)").match(self.s, pos)
            if by:  # sum by (x) ( ... )
                pos = by.end()
            end = self.matching_paren(pos)
            self.leaves.append(self.s[self.i:end])
            self.i = end
            out = f"query{len(self.leaves)}"

        zero = OR_ZERO.match(self.s, self.i)
        if zero:  # "no data -> 0" exists in Datadog formulas as default_zero()
            self.i = zero.end()
            out = f"default_zero({out})"
        return out

    def matching_paren(self, pos):
        while pos < len(self.s) and self.s[pos].isspace():
            pos += 1
        if pos >= len(self.s) or self.s[pos] != "(":
            raise Unsupported(f"expected '(' at: '{self.s[pos:]}'")
        depth, in_str = 0, False
        for j in range(pos, len(self.s)):
            c = self.s[j]
            if c == '"' and self.s[j - 1] != "\\":
                in_str = not in_str
            elif not in_str and c == "(":
                depth += 1
            elif not in_str and c == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
        raise Unsupported("unbalanced brackets")


# ---------------------------------------------------------------------------
# Step 2: parse one leaf
#
#   agg [by (l1, l2)] ( rate|increase ( {matchers} [window] ) )
#   histogram_quantile ( q , <the shape above with rate> )
# ---------------------------------------------------------------------------

QUANTILE = re.compile(r"^\s*histogram_quantile\s*\(\s*([\d.]+)\s*,(.*)\)\s*$", re.S)
OUTER = re.compile(r"^\s*(\w+)\s*(?:by\s*\(([^)]*)\))?\s*\((.*)\)\s*$", re.S)
INNER = re.compile(
    r"^\s*(rate|increase)\s*\(\s*([\w.]*)\s*\{(.*)\}\s*\[([^\]]+)\]\s*(?:offset\s+(\d+)([smhd]))?\s*\)\s*$", re.S
)
TOPK = re.compile(r"^\s*(topk|bottomk)\s*\(\s*(\d+)\s*,(.*)\)\s*$", re.S)
MATCHER = re.compile(r'\s*([\w.]+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')


def parse_leaf(text):
    quantile = None
    q = QUANTILE.match(text)
    if q:
        quantile, text = q.group(1), q.group(2)

    outer = OUTER.match(text)
    if not outer or outer.group(1) not in AGGREGATORS:
        raise Unsupported(f"query shape not supported: {text.strip()}")
    agg, by, inner_text = outer.groups()

    inner = INNER.match(inner_text)
    if not inner:
        raise Unsupported(f"inner expression not supported: {inner_text.strip()}")
    fn, metric_name, selector, window, off_n, off_unit = inner.groups()

    matchers, pos = [], 0
    while pos < len(selector):
        m = MATCHER.match(selector, pos)
        if not m:
            raise Unsupported(f"cannot parse selector: {{{selector}}}")
        matchers.append(m.groups())
        pos = m.end()
        if pos < len(selector) and selector[pos] == ",":
            pos += 1
    if metric_name:  # classic form: metric_name{...}
        matchers.append(("otel_metric_name", "=", metric_name))

    return {
        "quantile": quantile,
        "agg": agg,
        "by": [b.strip() for b in by.split(",")] if by else [],
        "fn": fn,
        "window": window.strip(),
        "offset": int(off_n) * OFFSET_UNITS[off_unit] if off_n else 0,
        "matchers": matchers,
    }


# ---------------------------------------------------------------------------
# Step 3: translate one parsed leaf into a Datadog query
# ---------------------------------------------------------------------------


def dd_tag_value(value):
    """Datadog stores tag values lowercase, with spaces turned into underscores."""
    return value.lower().replace(" ", "_")


def map_metric(p, notes):
    """Dash0 span metric + kind/status labels -> Datadog trace metric name."""
    labels = {k: v for k, op, v in p["matchers"] if k in METRIC_LABELS and op == "="}
    name = labels.get("otel_metric_name", "")
    kind = labels.get("otel_span_kind", "")
    status = labels.get("otel_span_status_code", "")

    if kind == "SERVER":
        operation = SERVER_OPERATION
    elif kind in ("", "INTERNAL"):
        operation = INTERNAL_OPERATION
        notes.append(f"internal spans assumed to be Datadog operation '{INTERNAL_OPERATION}'")
    else:
        raise Unsupported(f"span kind '{kind}' has no rule")

    by_status = ".by_http_status" if STATUS_LABEL in p["by"] else ""
    if by_status and kind != "SERVER":
        raise Unsupported("HTTP status breakdown only exists for server spans in Datadog")
    if name == "dash0.spans" and p["quantile"] is None:
        if status == "ERROR":
            return f"trace.{operation}.errors{by_status}"
        if status == "":
            return f"trace.{operation}.hits{by_status}"
    if name == "dash0.spans.duration" and p["quantile"] is not None and status == "":
        return f"trace.{operation}"  # distribution metric, in seconds
    raise Unsupported(
        f"no Datadog metric for {name!r} (span_kind={kind!r}, status={status!r}, "
        f"quantile={p['quantile']!r})"
    )


def map_filters(matchers, notes, variables):
    """Returns Datadog filter terms. OR groups force explicit AND between terms."""
    terms, boolean = [], False
    for key, op, value in matchers:
        if key in METRIC_LABELS:
            continue
        dd_key = LABELS.get(key)
        if dd_key is None:
            dd_key = key
            notes.append(f"label '{key}' not in table, kept as-is")

        var = re.fullmatch(r"\$(\w+)", value)
        if var and op == "=~":
            # dashboard variable: Datadog expands "$name" to "<prefix>:<value>"
            if var.group(1) not in variables:
                raise Unsupported(f"variable ${var.group(1)} is not defined")
            if variables[var.group(1)] != dd_key:
                raise Unsupported(f"variable ${var.group(1)} filters a different tag than '{dd_key}'")
            terms.append(f"${var.group(1)}")
            continue

        if op in ("=", "!="):
            v = dd_tag_value(value)
            if v != value:
                notes.append(f"tag value normalized: '{value}' -> '{v}'")
            terms.append(("!" if op == "!=" else "") + f"{dd_key}:{v}")
        elif op == "=~" and re.fullmatch(r"[\w./ -]+(\|[\w./ -]+)+", value):
            # a|b|c  ->  (k:a OR k:b OR k:c)
            options = [f"{dd_key}:{dd_tag_value(v)}" for v in value.split("|")]
            terms.append("(" + " OR ".join(options) + ")")
            boolean = True
        elif op == "=~" and re.fullmatch(r"[\w./ -]*(\.\*[\w./ -]*)+", value):
            terms.append(f"{dd_key}:{dd_tag_value(value).replace('.*', '*')}")
        else:
            raise Unsupported(f'matcher {key}{op}"{value}" not supported')

    if not terms:
        return "*"
    return (" AND " if boolean else ",").join(terms)


def map_function(p, widget_type):
    """Returns (Datadog suffix, scalar aggregator for query_value widgets)."""
    fn, window = p["fn"], p["window"]
    if p["quantile"] is not None:
        if fn == "rate":  # rate() inside histogram_quantile only feeds the histogram
            return "", "avg"
    elif fn == "rate":
        return ".as_rate()", "avg"
    elif fn == "increase" and window == "$__range" and widget_type in SCALAR_WIDGETS:
        return ".as_count()", "sum"
    elif fn == "increase" and window == "$__interval":
        return ".as_count()", "sum"
    raise Unsupported(f"{fn}(...[{window}]) in {widget_type} has no rule")


def translate_leaf(text, widget_type, notes, variables):
    p = parse_leaf(text)
    metric = map_metric(p, notes)
    filters = map_filters(p["matchers"], notes, variables)
    suffix, aggregator = map_function(p, widget_type)
    by = [LABELS.get(b, b) for b in p["by"]]

    if p["quantile"] is not None:
        prefix = QUANTILES.get(p["quantile"])
        if prefix is None:
            raise Unsupported(f"quantile {p['quantile']} has no Datadog percentile")
        if widget_type in SCALAR_WIDGETS:
            notes.append(
                "percentile as a single number: Datadog averages the p-value per time slot, "
                "Dash0 computes it over the whole window (numbers can differ a lot)"
            )
    else:
        prefix = p["agg"]

    query = f"{prefix}:{metric}{{{filters}}}"
    if by:
        query += f" by {{{','.join(by)}}}"
    return query + suffix, aggregator, p["offset"]


# ---------------------------------------------------------------------------
# Panel -> widget
# ---------------------------------------------------------------------------


def convert_panel(name, panel, report, variables):
    spec = panel["spec"]
    title = spec.get("display", {}).get("name", name)
    kind = spec["plugin"]["kind"]
    widget_type = PANEL_TYPES.get(kind)

    if kind not in DASH0_PANEL_TYPES:
        report.add("FAIL", title, [f"'{kind}' is not a Dash0 panel type; the source dashboard itself is broken"])
        return placeholder(title, f"Source panel type {kind} is not valid in Dash0.")

    if widget_type is None:
        report.add("FAIL", title, [f"panel type '{kind}' has no Datadog equivalent"])
        return placeholder(title, f"Panel type {kind} was not migrated.")

    if widget_type == "note":
        report.add("OK", title)
        return {"type": "note", "content": spec["plugin"]["spec"].get("text", "")}

    notes, queries, formulas = [], [], []
    try:
        for q in spec.get("queries", []):
            qspec = q["spec"]["plugin"]
            if qspec["kind"] != "PrometheusTimeSeriesQuery":
                raise Unsupported(f"query kind '{qspec['kind']}' not supported")

            promql = qspec["spec"]["query"]
            limit = None
            top = TOPK.match(promql)
            if top:  # topk(5, X) -> show the 5 highest rows
                limit = {"count": int(top.group(2)), "order": "desc" if top.group(1) == "topk" else "asc"}
                promql = top.group(3)
                if widget_type != "query_table":
                    notes.append(f"{top.group(1)} only limits rows in tables; ignored for {widget_type}")
                    limit = None

            parser = ExprParser(promql)
            formula = parser.parse()
            notes.extend(parser.notes)

            # Number the queries across the whole panel: query1, query2, ...
            offset = len(queries)
            for i in range(len(parser.leaves), 0, -1):
                formula = re.sub(rf"\bquery{i}\b", f"query{i + offset}", formula)

            for i, leaf in enumerate(parser.leaves, start=offset + 1):
                dd_query, aggregator, shift = translate_leaf(leaf, widget_type, notes, variables)
                entry = {"data_source": "metrics", "name": f"query{i}", "query": dd_query}
                if widget_type in SCALAR_WIDGETS:
                    entry["aggregator"] = aggregator
                queries.append(entry)
                if shift:  # offset 1h -> timeshift(query, -3600)
                    formula = re.sub(rf"\bquery{i}\b", f"timeshift(query{i}, -{shift})", formula)

            f = {"formula": formula}
            if limit:
                f["limit"] = limit
            alias = qspec["spec"].get("seriesNameFormat")
            if alias and "{{" not in alias and "$" not in alias:
                f["alias"] = alias
            elif widget_type == "query_table":
                # A template like {{otel_span_name}} names rows, not the value column.
                f["alias"] = title
            if widget_type == "timeseries" and re.search(r"\brate\s*\(", promql):
                notes.append(
                    "line shape: Dash0 smooths rate() over a window, Datadog computes each "
                    "time slot on its own (same totals, spikier line)"
                )
            formulas.append(f)
    except Unsupported as e:
        report.add("FAIL", title, [str(e)])
        return placeholder(title, f"Not migrated: {e}")

    request = {
        "response_format": "scalar" if widget_type in SCALAR_WIDGETS else "timeseries",
        "queries": queries,
        "formulas": formulas,
    }
    if widget_type == "timeseries":
        request["display_type"] = "line"

    widget = {"title": title, "type": widget_type, "requests": [request]}
    if widget_type == "query_value":
        unit = spec["plugin"]["spec"].get("format", {}).get("unit")
        widget.update({"autoscale": True, "precision": 1 if unit == "percent" else 0})
        if unit == "percent":
            widget["custom_unit"] = "%"

    report.add("WARN" if notes else "OK", title, notes)
    return widget


def placeholder(title, text):
    return {
        "type": "note",
        "content": f"**{title}**\n\n⚠️ {text}",
        "background_color": "yellow",
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def convert(perses):
    if perses.get("kind") != "PersesDashboard":
        raise SystemExit("Input is not a PersesDashboard.")

    spec = perses["spec"]
    report = Report()
    panels = spec.get("panels", {})

    # Layout items in reading order (top-to-bottom, left-to-right).
    items = []
    for layout in spec.get("layouts", []):
        items.extend(layout.get("spec", {}).get("items", []))
    items.sort(key=lambda it: (it.get("y", 0), it.get("x", 0)))

    variables, template_variables = convert_variables(spec.get("variables", []), report)

    widgets, placed = [], set()
    for item in items:
        name = item["content"]["$ref"].split("/")[-1]
        if name not in panels:
            report.add("FAIL", name, ["layout points to a missing panel"])
            continue
        placed.add(name)
        widgets.append({
            "definition": convert_panel(name, panels[name], report, variables),
            # Perses grid: 24 columns. Datadog grid: 12 columns.
            "layout": {
                "x": item.get("x", 0) // 2,
                "y": item.get("y", 0) // 3,
                "width": max(1, item.get("width", 12) // 2),
                "height": max(1, item.get("height", 6) // 3),
            },
        })

    for name in sorted(set(panels) - placed):
        report.add("WARN", name, ["panel not in any layout, skipped"])

    display = spec.get("display", {})
    title = display.get("name", perses.get("metadata", {}).get("name", "Migrated dashboard"))
    dashboard = {
        "title": title.replace("Dash0", "Datadog"),
        "description": display.get("description", ""),
        "layout_type": "ordered",
        "widgets": widgets,
    }
    if template_variables:
        dashboard["template_variables"] = template_variables
    return dashboard, report


def convert_variables(perses_vars, report):
    """Perses list variables over a label -> Datadog template variables over a tag.

    Returns ({name: datadog_tag}, [template_variables]).
    """
    mapping, out = {}, []
    for v in perses_vars:
        spec = v.get("spec", {})
        name = spec.get("name", "?")
        plugin = spec.get("plugin", {})
        if v.get("kind") != "ListVariable" or plugin.get("kind") != "PrometheusLabelValuesVariable":
            report.add("FAIL", f"variable ${name}", [f"variable type {v.get('kind')}/{plugin.get('kind')} has no rule"])
            continue
        label = plugin["spec"]["labelName"]
        tag = LABELS.get(label, label)
        mapping[name] = tag
        out.append({"name": name, "prefix": tag, "available_values": [], "defaults": ["*"]})
        report.add("WARN", f"variable ${name}", [
            f"list of '{label}' values -> Datadog tag '{tag}'",
            "values differ in spelling (Datadog lowercases them); default set to 'all'",
        ])
    return mapping, out


def main():
    ap = argparse.ArgumentParser(description="Convert a Perses (Dash0) dashboard to Datadog JSON.")
    ap.add_argument("input", help="Perses dashboard JSON file")
    ap.add_argument("-o", "--output", help="Datadog dashboard JSON file (default: stdout)")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        perses = json.load(f)

    dashboard, report = convert(perses)
    text = json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(text)

    report.print()
    sys.exit(1 if any(s == "FAIL" for s, _, _ in report.lines) else 0)


if __name__ == "__main__":
    main()
