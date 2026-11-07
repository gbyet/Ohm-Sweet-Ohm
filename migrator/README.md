# Migrator: Datadog ⇄ Dash0

Plain Python 3, no dependencies. Deterministic: same input, same output, no LLM.

| Script | Input | Output |
|---|---|---|
| `datadog_to_perses.py` | Datadog **dashboard** JSON | Dash0 dashboard (Perses + PromQL) |
| `monitors_to_dash0.py` | Datadog **monitors** JSON | Dash0 check rules (Prometheus alert rules) |
| `perses_to_datadog.py` | Dash0 dashboard JSON | Datadog dashboard JSON (reverse direction) |

```bash
python3 datadog_to_perses.py  dashboard.json  -o dash0-dashboard.json
python3 monitors_to_dash0.py  monitors.json   -o dash0-check-rules.json
python3 perses_to_datadog.py  dash0.json      -o datadog-dashboard.json
```

Export a dashboard from Datadog: Dashboard → ⚙ → Export JSON.
Import into Dash0: Dashboards → + Add → Edit as JSON → paste.

Every widget and monitor gets one line in the report: ✅ migrated, ⚠️ migrated with a noted
difference, ❌ not migrated (with the reason). Nothing is dropped silently.

`datadog_metrics.csv.gz` is Datadog's metric catalog (type and unit of ~37,000 metrics). The
dashboard parser needs it to pick the right PromQL (`rate`, `increase`, plain gauge). Keep it next
to the scripts.

Tested on all 516 dashboards and 329 monitors in `DataDog/integrations-core`: 82 % of widgets and
74 % of monitors migrate, 0 crashes.
