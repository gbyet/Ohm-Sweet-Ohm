import { trace } from "@opentelemetry/api"
import { PRODUCTS, chaos, fail, log, sleep, step } from "@/lib/shop"

export async function POST(req: Request) {
  const { items } = (await req.json()) as { items: string[] }
  const total = items.reduce(
    (sum, id) => sum + (PRODUCTS.find((p) => p.id === id)?.price ?? 0),
    0
  )
  trace
    .getActiveSpan()
    ?.setAttributes({
      "shop.item_count": items.length,
      "shop.order_total": total,
    })

  if (items.length === 0) return fail(400, "Cart is empty")

  try {
    await step("inventory.reserve", async () => {
      await sleep(60 + Math.random() * 100)
    })

    await step("payment.charge", async (span) => {
      span.setAttribute("shop.order_total", total)
      // Slow checkout: the payment provider takes 2–4 seconds.
      await sleep(
        chaos.slowCheckout
          ? 2000 + Math.random() * 2000
          : 100 + Math.random() * 200
      )
      if (chaos.paymentOutage)
        throw new Error("Payment provider returned 502 Bad Gateway")
    })

    await step("shipping.create_label", async () => {
      await sleep(50 + Math.random() * 80)
    })
  } catch (err) {
    return fail(502, `Checkout failed: ${(err as Error).message}`, {
      "shop.order_total": total,
    })
  }

  const orderId = `ORD-${Math.random().toString(36).slice(2, 8).toUpperCase()}`
  log("info", `Order placed ${orderId}`, {
    "shop.order_id": orderId,
    "shop.order_total": total,
  })
  return Response.json({ orderId, total })
}
