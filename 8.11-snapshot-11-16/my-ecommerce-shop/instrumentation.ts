// Runs once when the Next.js server starts.
// Sends the same traces and logs to every configured backend (Dash0 and/or Datadog).
import { registerOTel, OTLPHttpProtoTraceExporter } from "@vercel/otel"
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base"
import type { LogRecordProcessor } from "@opentelemetry/sdk-logs"

type Backend = { name: string; url: string; headers: Record<string, string> }

function backends(): Backend[] {
  const list: Backend[] = []
  const env = process.env

  if (env.DASH0_OTLP_ENDPOINT && env.DASH0_AUTH_TOKEN) {
    list.push({
      name: `Dash0 (dataset: ${env.DASH0_DATASET ?? "default"})`,
      url: env.DASH0_OTLP_ENDPOINT,
      headers: {
        Authorization: `Bearer ${env.DASH0_AUTH_TOKEN}`,
        "Dash0-Dataset": env.DASH0_DATASET ?? "default",
      },
    })
  }

  if (env.DD_OTLP_ENDPOINT && env.DD_API_KEY) {
    list.push({
      name: "Datadog",
      url: env.DD_OTLP_ENDPOINT,
      // compute_stats: lets Datadog build APM metrics (request rate, errors, latency) from the traces.
      headers: { "dd-api-key": env.DD_API_KEY, compute_stats: "true" },
    })
  }

  return list
}

export async function register() {
  const targets = backends()

  if (targets.length === 0) {
    console.warn(
      "[telemetry] No backend configured. Telemetry is NOT sent anywhere."
    )
    registerOTel({ serviceName: "glitchy-shop" })
    return
  }

  const logRecordProcessors: LogRecordProcessor[] = []

  // The logs exporter only works in the Node.js runtime.
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { OTLPLogExporter } =
      await import("@opentelemetry/exporter-logs-otlp-http")
    const { BatchLogRecordProcessor } = await import("@opentelemetry/sdk-logs")
    for (const t of targets) {
      logRecordProcessors.push(
        new BatchLogRecordProcessor({
          exporter: new OTLPLogExporter({
            url: `${t.url}/v1/logs`,
            headers: t.headers,
          }),
        })
      )
    }
  }

  registerOTel({
    serviceName: "glitchy-shop",
    attributes: { "deployment.environment.name": "demo" },
    spanProcessors: targets.map(
      (t) =>
        new BatchSpanProcessor(
          new OTLPHttpProtoTraceExporter({
            url: `${t.url}/v1/traces`,
            headers: t.headers,
          })
        )
    ),
    logRecordProcessors,
  })

  for (const t of targets)
    console.log(`[telemetry] Sending traces + logs to ${t.name}`)
}
