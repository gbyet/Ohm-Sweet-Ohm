# 💸 A Proprietary Monitoring Platform → Dash0

Your dashboards are trapped in a platform with vendor lock-in and JSON only its mother could love. This should help: 

https://dash0-migrator.vercel.app

## How we proceeded

1. We began by understanding how each platform stores a dashboard as JSON. To see it, we built the smallest demo possible case. [My e-commerce shop](my-ecommerce-shop/) is a demo ecommerce shop which sends traces with OpenTelemetry to Dash0 and to DataDog. In each platform we built a dashboard with two panels which tracks the numbers of requests and the requests per second and saved its JSON in [dashboards](my-ecommerce-shop/dashboards/).

   *Run the shop yourself:* `cd my-ecommerce-shop && npm install && npm run dev`


2. We put the two JSON files side by side and compared their structure ([dashboards](my-ecommerce-shop/dashboards/)). Datadog keeps everything about a box in one widget: its chart type, its query and its position. Perses splits this in two. `panels` hold the chart type and the query, and `layouts` hold the positions and point to the panels by name:

```
DataDog            Dash0 (Perses)
widgets                          panels
├── box 1: Requests              ├── requestsStat  → StatChart + query
└── box 2: Requests per second   └── requestRate   → TimeSeriesChart + query
                                 layouts
                                 ├── position → requestsStat
                                 └── position → requestRate
```

3. We scaled up to real production dashboards. With Claude Fable we went through the platform's own repository of 516 dashboards and 329 alerts ([integrations-core](https://github.com/DataDog/integrations-core)) and tested each one. The [converter](converter/) embeds these rules as plain code. Example of three rules: 

```
timeseries widget                → TimeSeriesChart panel
query_value widget               → StatChart panel
sum:requests{service:shop}.as_rate()  → sum (rate({otel_metric_name="requests", service_name="shop"}[$__rate_interval]))
```

   Result: 85 % of widgets and 74 % of alerts convert, with 0 crashes.

4. Put the converter on a website, so nobody has to read step 3.

## Initial limitation, solved

A migration has two parts: **dashboards**, the numbers such as requests per second, and **alerts**, the workflow that warns you. At first the website only converted: you copied the result and imported it in Dash0 by hand. That works for dashboards, but Dash0 has no file import for alerts.

**Now** the website sends both straight to Dash0 through its import API ([dashboards](https://www.dash0.com/docs/api-reference/operations/post/api/import/dashboard), [alerts](https://www.dash0.com/docs/api-reference/operations/post/api/import/check-rules)). Paste a Dash0 auth token, pick the dataset, send. Copy and download still work.

## Files

```
deck-final-v17.pdf           pitch slides
converter/
├── datadog_to_perses.py     Datadog dashboard → Dash0 dashboard (Perses)
├── monitors_to_dash0.py     Datadog monitor → Dash0 alert (Prometheus rule)
└── datadog_metrics.csv.gz   type and unit of 36,948 Datadog metrics, used by both
my-ecommerce-shop/           demo shop that sends traces to Dash0 and Datadog
├── instrumentation.ts       sends the traces with OpenTelemetry
├── loadgen.py               simulated shoppers, so the dashboards have data
└── dashboards/
    ├── datadog.json         the Datadog dashboard
    ├── dash0.json           the same dashboard in Dash0
    ├── structure.txt        the two structures side by side
    └── about.txt            what the two files are
```

<3 Group OhmSweetOhm by Eric, Jonas, David and Maggie. <3 