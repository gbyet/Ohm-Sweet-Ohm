// Server-side "backend" of the glitchy shop: products, chaos switches, and telemetry helpers.
import { trace, SpanStatusCode, type Span } from "@opentelemetry/api"
import { logs, SeverityNumber } from "@opentelemetry/api-logs"

export type Product = {
  id: string
  name: string
  price: number
  stock: number
  color: string // Tailwind classes for the product tile
  emoji: string
}

export const PRODUCTS: Product[] = [
  {
    id: "sneakers",
    name: "Neon Sneakers",
    price: 89,
    stock: 12,
    color: "bg-pink-100 dark:bg-pink-950",
    emoji: "👟",
  },
  {
    id: "headphones",
    name: "Studio Headphones",
    price: 149,
    stock: 4,
    color: "bg-sky-100 dark:bg-sky-950",
    emoji: "🎧",
  },
  {
    id: "backpack",
    name: "Trail Backpack",
    price: 64,
    stock: 20,
    color: "bg-emerald-100 dark:bg-emerald-950",
    emoji: "🎒",
  },
  {
    id: "watch",
    name: "Smart Watch",
    price: 219,
    stock: 2,
    color: "bg-amber-100 dark:bg-amber-950",
    emoji: "⌚",
  },
  {
    id: "camera",
    name: "Instant Camera",
    price: 99,
    stock: 7,
    color: "bg-violet-100 dark:bg-violet-950",
    emoji: "📷",
  },
  {
    id: "mystery",
    name: "Mystery Box",
    price: 25,
    stock: 99,
    color: "bg-orange-100 dark:bg-orange-950",
    emoji: "🎁",
  },
]

export type Chaos = {
  flakyCart: boolean // "Add to cart" fails ~30% of the time
  slowCheckout: boolean // checkout takes 2–4 seconds
  paymentOutage: boolean // payment always fails
  brokenMystery: boolean // the Mystery Box always crashes
}

// Kept in server memory. Resets when the server restarts.
const g = globalThis as unknown as { __chaos?: Chaos }
g.__chaos ??= {
  flakyCart: true,
  slowCheckout: false,
  paymentOutage: false,
  brokenMystery: true,
}
export const chaos = g.__chaos

export const tracer = trace.getTracer("glitchy-shop")
const logger = logs.getLogger("glitchy-shop")

export function log(
  level: "info" | "warn" | "error",
  message: string,
  attributes: Record<string, string | number | boolean> = {}
) {
  const severity = {
    info: SeverityNumber.INFO,
    warn: SeverityNumber.WARN,
    error: SeverityNumber.ERROR,
  }[level]
  logger.emit({
    severityNumber: severity,
    severityText: level.toUpperCase(),
    body: message,
    attributes,
  })
  console[level === "info" ? "log" : level](`[${level}] ${message}`, attributes)
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** Runs `fn` inside a child span. Exceptions mark the span as failed. */
export async function step<T>(
  name: string,
  fn: (span: Span) => Promise<T>
): Promise<T> {
  return tracer.startActiveSpan(name, async (span) => {
    try {
      return await fn(span)
    } catch (err) {
      span.recordException(err as Error)
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (err as Error).message,
      })
      throw err
    } finally {
      span.end()
    }
  })
}

/** Marks the current request span as failed and returns a JSON error response. */
export function fail(
  status: number,
  message: string,
  attributes: Record<string, string | number | boolean> = {}
) {
  const span = trace.getActiveSpan()
  span?.setStatus({ code: SpanStatusCode.ERROR, message })
  span?.setAttributes({ "shop.error": message, ...attributes })
  log("error", message, { "http.response.status_code": status, ...attributes })
  return Response.json({ error: message }, { status })
}
