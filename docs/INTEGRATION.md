# Integration Dash0 und Datadog: Vorgehen und Erkenntnisse

THINC Hackathon München 2027, Dash0 Challenge „Breaking Observability Vendor Lock-In“
Stand: 7. November 2026

Dieses Dokument beschreibt Schritt für Schritt, wie die Verbindung zwischen Dash0, Datadog und einer lokalen Perses-Instanz aufgebaut wurde, welche Probleme dabei auftraten und was daraus für den eigentlichen Migrator folgt.

> **Wichtig zur Richtung:** Die Challenge verlangt einen Migrator **von Datadog nach Dash0**. Alles, was hier beschrieben ist, lief überwiegend in die **Gegenrichtung** (Dash0 nach Datadog). Das Ziel war eine Testumgebung: ein Datadog-Konto mit einem Dashboard und echten Daten, das der Migrator später als Quelle auslesen kann, und daneben das Dash0-Dashboard als erwartetes Ergebnis.

---

## 1. Überblick

```
OpenTelemetry Demo (Docker, Mac)
        │  OTLP
        ▼
      Dash0  ──── Prometheus-API ────►  Kopierskript  ──── Datadog-API ────►  Datadog
        │                              (dash0_to_datadog_all.py)
        │  Dashboard-Export (Perses)
        ▼
  lokale Perses-Instanz (Test)         Dashboard-Konvertierung  ──────────►  Datadog-Dashboard
                                       (dash0_postgres_to_datadog.py)
```

| Baustein | Zweck | Ort |
|---|---|---|
| Perses v0.55.1 + Prometheus v3.16.0 | lokale Testumgebung für Perses-Dashboards | `perses\` |
| Datadog-Beispieldateien | originale Dashboards und Monitore von Datadog | `datadog-samples\` |
| Dash0-Export | das Postgres-Dashboard aus Dash0 | `dash0-samples\` |
| Skripte | Konvertierung, Upload, Datenkopie | `scripts\` |
| Zugangsdaten | API-Schlüssel, nur lokal | `.env` (per `.gitignore` von Git ausgeschlossen) |

---

## 2. Lokale Perses-Instanz

Docker war auf dem Rechner nicht installiert. Statt Docker Desktop wurden deshalb die fertigen Windows-Programme von GitHub verwendet: Perses v0.55.1 und Prometheus v3.16.0.

Start und Stopp laufen über `perses\start.ps1` und `perses\stop.ps1`. Perses ist danach unter `http://localhost:8080` erreichbar, Prometheus unter `http://localhost:9090`.

**Stolperstein:** Perses blockiert aus Sicherheitsgründen Datenquellen auf dem eigenen Rechner (Schutz vor Server-Side Request Forgery). In `perses\perses\config.yaml` wurde deshalb gezielt nur `127.0.0.1/32` freigegeben:

```yaml
datasource:
  proxy:
    allowed_networks:
      - "127.0.0.1/32"
```

Die offizielle Anleitung nutzt als Adresse `http://prometheus:9090`. Diesen Namen gibt es nur im Docker-Netzwerk, lokal muss es `http://127.0.0.1:9090` heißen.

Angelegt wurden die Projekte `my-first-project` und `thinc_ohh_sweet`, jeweils mit einer Prometheus-Datenquelle.

---

## 3. Datadog-Beispielinhalte

Aus dem öffentlichen Repository `DataDog/integrations-core` wurden Testdateien geladen:

| Datei | Inhalt | Bedeutung für den Migrator |
|---|---|---|
| `redis\dashboard_overview.json` | Redis Overview, 37 Widgets | guter Haupttestfall, enthält Formeln und Logs-Widgets |
| `redis\monitor_high_mem.json` | Alarm bei über 70 % bzw. 90 % Speicher | Alarm mit zwei Schwellen |
| `postgres\dashboard_postgres_overview.json` | Postgres Overview, 40 Widgets | Original zum Dash0-Dashboard |
| `postgres\monitor_percent_usage_connections.api.json` | Alarm Verbindungsauslastung | einfacher, sauber übersetzbarer Fall |
| `postgres\monitor_replication_delay.api.json` | Alarm mit `anomalies(...)` | nicht übersetzbar, muss im Log markiert werden |

Die Monitor-Dateien liegen im Paketformat von Datadog vor (`"version": 2`, eigentlicher Monitor unter `definition`). Die `.api.json`-Varianten enthalten nur den inneren Teil, so wie ihn die Datadog-API erwartet.

---

## 4. Dash0-Dashboard in Perses importieren

Das Postgres-Dashboard wurde aus Dash0 exportiert (alternativ per CLI: `dash0 dashboards get <id> -o json`) und unter `dash0-samples\postgres-overview.dash0.json` gespeichert.

**Erkenntnis 1: Dash0 nutzt ein erweitertes Perses-Format.** Der Import in Standard-Perses schlug zunächst fehl, weil jede Abfrage zwei zusätzliche Felder enthält, die Perses nicht kennt:

```json
"spec": { "hidden": false, "id": "d4d13667-...", "query": "...", "seriesNameFormat": "..." }
```

Nach dem Entfernen von 60 dieser Felder lief der Import durch. Außerdem musste die Hülle angepasst werden: Dash0 exportiert als Kubernetes-Objekt (`apiVersion: perses.dev/v1alpha1`, `kind: PersesDashboard`), die Perses-API erwartet `kind: Dashboard` mit `metadata.project`. Die bereinigte Fassung liegt unter `dash0-samples\postgres-overview.perses.json`.

**Erkenntnis 2: Dash0 fragt Metriken über ihren OpenTelemetry-Namen ab**, nicht über einen Prometheus-Namen:

```promql
sum by (postgresql_database_name) (rate({otel_metric_name="postgresql.operations", operation="ins"}[$__rate_interval]))
```

Genau dieses Format muss der Migrator als Zielformat erzeugen.

---

## 5. Dash0-Dashboard nach Datadog konvertieren

Skript: `scripts\dash0_postgres_to_datadog.py`
Ergebnis: `datadog-samples\postgres\dashboard_postgres_from_dash0.json` (7 Gruppen, 23 Widgets)

Die Übersetzung arbeitet bewusst **regelbasiert und ohne LLM**, so wie es die Challenge verlangt. Alle Datadog-Metriknamen wurden gegen die offizielle Liste `postgres/metadata.csv` aus `integrations-core` geprüft.

### Abbildungsregeln

| Dash0 / Perses | Datadog |
|---|---|
| `{otel_metric_name="postgresql.backends"}` | `postgresql.connections` |
| `postgresql.operations` mit `operation="ins"` / `upd` / `del` / `hot_upd` | `postgresql.rows_inserted` / `rows_updated` / `rows_deleted` / `rows_hot_updated` |
| `postgresql.blks_hit` / `blks_read` | `postgresql.buffer_hit` / `disk_read` |
| `postgresql.rows` mit `state="dead"` / `"live"` | `postgresql.dead_rows` / `live_rows` |
| Label `postgresql_database_name` / `_table_name` / `_index_name` | Tag `db` / `table` / `index` |
| `topk(10, …)` / `bottomk(10, …)` | `top(query1, 10, 'mean', 'desc')` / `'asc'` |
| StatChart / TimeSeriesChart / TimeSeriesTable | query_value / timeseries / query_table |
| Schwellen 70 / 90 | Farbregeln gelb / rot |
| Raster mit 24 Spalten | Raster mit 12 Spalten (x und Breite halbiert) |

### Semantische Unterschiede

Das sind genau die Fallen, vor denen die Challenge warnt: Eine Abfrage kann syntaktisch korrekt aussehen und trotzdem etwas anderes berechnen.

1. **Zähler gegen Raten:** In OpenTelemetry sind `rows_inserted` und ähnliche Metriken stetig steigende Zähler, die `rate()` brauchen. Datadog liefert dieselben Werte bereits als Rate pro Sekunde. Ein `rate()` darf auf Datadog-Seite also nicht noch einmal angewendet werden.
2. **Label gegen eigene Metrik:** OpenTelemetry nutzt eine Metrik mit Label (`type="scheduled"`), Datadog eine eigene Metrik pro Wert (`checkpoints_timed`, `checkpoints_requested`).
3. **Deadlocks:** Dash0 zeigt die Anzahl pro Intervall (`increase`), Datadog eine Rate. Die Kurvenform ist gleich, die Zahlen sind anders skaliert.

---

## 6. Upload nach Datadog

Skript: `scripts\datadog_import_dashboard.py`

Das Dashboard wird über `POST /api/v1/dashboard` hochgeladen. Dafür sind **zwei** Schlüssel nötig, was im Challenge-PDF nicht erwähnt wird:

| Schlüssel | Header | Format |
|---|---|---|
| API Key | `DD-API-KEY` | 32 Zeichen, ohne Bindestriche |
| Application Key | `DD-APPLICATION-KEY` | neues Format `ddapp_…` |

**Stolperstein:** In der Schlüsselliste zeigt Datadog nur die **Key ID** (Format `xxxxxxxx-xxxx-…`). Der eigentliche Application Key erscheint nur einmal direkt nach dem Erstellen. Mit der ID antwortet die API mit `401 Unauthorized`.

Der API Key lässt sich vorab mit `GET /api/v1/validate` prüfen. Das Konto liegt auf der EU-Seite, die API-Adresse lautet daher `https://api.datadoghq.eu`.

Das Dashboard liegt aktuell unter der ID `pfs-j5f-um2` („Postgres - Overview (from Dash0)“). Die zuerst hochgeladene Fassung wurde zwischenzeitlich gelöscht und manuell neu importiert, wodurch sich die ID geändert hat.

---

## 7. Daten von Dash0 nach Datadog

### 7.1 Region ermitteln

Dash0 unterscheidet API-Adressen nach Region. Zuerst wurde `api.eu-west-1.aws.dash0.com` verwendet. Diese Adresse antwortete zwar erfolgreich, zeigte aber nur 27 interne Dash0-Metriken. Erst ein Testwert, der über `ingress.europe-west4.gcp.dash0.com` gesendet und anschließend zurückgelesen wurde, bestätigte die richtige Region:

| Zweck | Adresse |
|---|---|
| Abfragen (Prometheus-API) | `https://api.europe-west4.gcp.dash0.com/api/prometheus/api/v1/...` |
| Daten senden (OTLP) | `https://ingress.europe-west4.gcp.dash0.com/v1/metrics` |
| Anmeldung | Header `Authorization: Bearer <Token>` |
| Datensatz | Parameter `dataset=default` bzw. Header `Dash0-Dataset` |

**Erkenntnis:** Eine erfolgreiche Antwort beweist nicht, dass man die richtige Region abfragt. Die Region sollte immer mit einem Schreib- und Lesetest geprüft werden.

### 7.2 Zwischenschritt: simulierte Postgres-Daten

Weil auf der falschen Region keine Postgres-Daten sichtbar waren, wurde vorübergehend ein Simulator gebaut (`scripts\postgres_data_generator.py`). Er erzeugte im Format des OpenTelemetry-Postgres-Receivers Werte für zwei erfundene Datenbanken (`shop`, `analytics`) und sendete sie per OTLP an Dash0.

Nachdem die richtige Region gefunden war, zeigten sich echte Daten. Der Simulator wurde daraufhin gestoppt. Zwischen etwa 17:37 und 17:50 Uhr liegen deshalb simulierte Werte in Dash0. Die echte Datenbank heißt `astronomy_db`.

### 7.3 Datenquelle in Dash0

In Dash0 liegen Daten der offiziellen **OpenTelemetry Demo** (ein Online-Shop aus rund 15 Microservices), betrieben in Docker Desktop auf einem Mac. Gemessen wurden am 7. November zwischen 15:45 und etwa 16:45 Uhr:

| Kennzahl | Wert |
|---|---|
| Metriken | 452 |
| davon Gauges / Counter / Histogramme | 218 / 184 / 84 (laut Dash0-Metadaten) |
| Zeitreihen | rund 4.900 in Dash0 |
| Bereiche | Postgres, Redis, Kafka, NGINX, JVM, Node.js, .NET, Go, Python, Container, Hosts, HTTP, RPC |
| Dashboards in Dash0 | 1 (Postgres) |
| Alarme (Check Rules) in Dash0 | 0 |

### 7.4 Kopierskript für alle Metriken

Skript: `scripts\dash0_to_datadog_all.py`

Ablauf pro Durchlauf (im Dauerbetrieb jede Minute):

1. Über `label/otel_metric_name/values` ermitteln, welche Metriken gerade Daten erhalten.
2. Über die Metadaten-API von Dash0 den Typ jeder Metrik bestimmen.
3. Jede Metrik einzeln abfragen und umrechnen:

| Typ in Dash0 | Abfrage | Ergebnis in Datadog |
|---|---|---|
| Gauge | `{otel_metric_name="X"}` | Gauge `X`, unverändert |
| Counter | `rate({...}[5m]) * 60` | Count `X`, Zuwachs pro Minute |
| Histogramm | `histogram_count(rate(...[5m])) * 60` und `histogram_sum(...)` | Count `X.count` und `X.sum` |
| Histogramm | `histogram_quantile(0.5 / 0.95 / 0.99, rate(...[5m]))` | Gauge `X.p50`, `X.p95`, `X.p99` |

4. Labels werden zu Datadog-Tags. Sehr individuelle oder interne Labels (Container-IDs, Betriebssystem-Texte, `dash0_*`) werden verworfen, damit die Zahl der Zeitreihen beherrschbar bleibt. `service_name` wird zu `service`, `host_name` zu `host`. Jede kopierte Reihe trägt den Tag `source:dash0-copy`.
5. Zusätzlich werden die Postgres-Werte unter den Datadog-Integrationsnamen gesendet (`postgresql.connections` usw.), damit das konvertierte Datadog-Dashboard Daten anzeigt.
6. Versand per `POST /api/v2/series` in Paketen unter 400 kB.

**Testergebnis** auf einem 5-Minuten-Fenster der Demo-Daten: 424 von 451 Metriken übertragen, rund 11.800 Zeitreihen in Datadog, keine Fehler. Die übrigen 27 Metriken hatten im Testfenster keine Daten.

### 7.5 Probleme beim Kopieren und ihre Lösung

| Problem | Ursache | Lösung |
|---|---|---|
| Nur 138 von 449 Metriken kamen an | Mehrere Metriken unterschiedlichen Typs in einer Abfrage liefern bei Dash0 leere Ergebnisse (Warnung „different metric types / temporality“) | jede Metrik einzeln abfragen, 8 Abfragen parallel |
| Regex-Filter liefern nichts | `{otel_metric_name=~"..."}` wird von Dash0 nicht zuverlässig aufgelöst | nur exakte Treffer `=` verwenden |
| Fehler „unknown escape sequence“ | Punkte im Regex müssen in PromQL doppelt maskiert werden | entfällt durch exakte Treffer |
| `increase(...[1m])` leer | Die Quellen senden nur etwa einmal pro Minute, ein 1-Minuten-Fenster enthält weniger als zwei Werte | 5-Minuten-Fenster: `rate(...[5m]) * 60` |
| Namenskonflikt | OTel und Datadog nutzen beide `postgresql.commits`, `rollbacks`, `deadlocks`, aber mit unterschiedlicher Bedeutung | für diese drei gilt die Datadog-Bedeutung, die OTel-Variante wird übersprungen |

### 7.6 Grenze von Datadog

Datadog nimmt Messpunkte nur bis etwa **eine Stunde in die Vergangenheit** an. Die Demo-Daten von 15:45 bis 16:45 Uhr lassen sich deshalb nachträglich nicht mehr übertragen. Das Skript kopiert beim Start die letzten 55 Minuten und danach laufend neue Daten. Sobald die OpenTelemetry Demo wieder an Dash0 sendet, erscheinen alle Metriken automatisch in Datadog.

---

## 8. Betrieb

| Aktion | Befehl |
|---|---|
| Perses starten / stoppen | `perses\start.ps1` / `perses\stop.ps1` |
| Datenkopie starten | `scripts\start_data_sync.ps1` |
| Datenkopie stoppen | `scripts\stop_data_sync.ps1` |
| Probelauf ohne Senden | `python scripts\dash0_to_datadog_all.py --dry-run` |
| Protokoll | `logs\copier_all.log` |

Nach einem Neustart des Rechners laufen weder Perses noch die Datenkopie automatisch weiter.

---

## 9. Sicherheit

Alle Zugangsdaten stehen ausschließlich in `.env`, die per `.gitignore` von Git ausgeschlossen ist. Datadog API Key, Datadog Application Key und Dash0 Auth Token wurden im Laufe der Einrichtung im Chat geteilt. **Alle drei sollten nach dem Hackathon widerrufen oder ersetzt werden.**

Rund 11.800 Zeitreihen zählen in Datadog als Custom Metrics. In der 14-tägigen Testphase ist das unproblematisch, in einem bezahlten Konto wäre es teuer.

---

## 10. Was daraus für den Migrator folgt

1. **Zielformat ist Dash0-Perses, nicht Standard-Perses.** Abfragen nutzen `{otel_metric_name="..."}`, und Dash0 ergänzt die Felder `id` und `hidden`. Wer mit Standard-Perses oder `percli` validiert, muss diese Unterschiede kennen.
2. **Metrik-Mapping braucht den Typ, nicht nur den Namen.** Datadog liefert viele Werte bereits als Rate, OpenTelemetry als Zähler. Die Mapping-Tabelle muss daher pro Metrik festhalten, ob auf Dash0-Seite `rate()` nötig ist.
3. **Labels und Metriken sind nicht 1:1 zuordenbar.** Ein Label-Wert in OpenTelemetry kann einer eigenen Metrik in Datadog entsprechen und umgekehrt.
4. **Nicht übersetzbare Funktionen müssen sichtbar markiert werden**, zum Beispiel `anomalies()` im Monitor zur Replikationsverzögerung oder Logs-Widgets im Redis-Dashboard.
5. **Die Mapping-Tabelle aus `dash0_postgres_to_datadog.py` ist umkehrbar** und kann als Startpunkt für die Richtung Datadog nach Dash0 dienen.
6. **Für die Demo** lassen sich beide Dashboards nebeneinander zeigen: gleiche Datenbank, gleiche Werte, einmal in Datadog und einmal in Dash0. Das ist der Nachweis semantischer Gleichwertigkeit, den die Challenge verlangt.

---

## Anhang: Dateien

```
thinc_hackathon_munich 2027\
├── .env                      Zugangsdaten (nicht teilen)
├── .gitignore
├── AGENT_PROMPT.md           Prompt für einen weiteren KI-Agenten
├── INTEGRATION.md            dieses Dokument
├── perses\                   lokale Perses- und Prometheus-Instanz
├── datadog-samples\
│   ├── redis\                Dashboard und Monitor aus integrations-core
│   └── postgres\             Original-Dashboard, Monitore, konvertiertes Dashboard
├── dash0-samples\            Dash0-Export und bereinigte Perses-Fassung
├── scripts\
│   ├── dash0_postgres_to_datadog.py   Dashboard-Konvertierung Dash0 nach Datadog
│   ├── datadog_import_dashboard.py    Dashboard-Upload nach Datadog
│   ├── dash0_to_datadog_data.py       Postgres-Mapping für Daten
│   ├── dash0_to_datadog_all.py        Datenkopie aller Metriken
│   ├── postgres_data_generator.py     Simulator (nicht mehr in Betrieb)
│   ├── start_data_sync.ps1
│   └── stop_data_sync.ps1
└── logs\                     Protokolle der Datenkopie
```
