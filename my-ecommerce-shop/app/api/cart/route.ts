import { trace } from "@opentelemetry/api"
import { PRODUCTS, chaos, fail, log, sleep, step } from "@/lib/shop"

export async function POST(req: Request) {
  const { id } = (await req.json()) as { id: string }
  const product = PRODUCTS.find((p) => p.id === id)
  trace.getActiveSpan()?.setAttribute("shop.product_id", id)
  if (!product)
    return fail(404, `Unknown product ${id}`, { "shop.product_id": id })

  try {
    await step("cart.add", async (span) => {
      span.setAttributes({ "shop.product_id": id, "shop.price": product.price })
      await sleep(40 + Math.random() * 120)
      if (chaos.flakyCart && Math.random() < 0.3) {
        throw new Error("Cart service timed out (redis connection reset)")
      }
    })
  } catch (err) {
    return fail(503, (err as Error).message, { "shop.product_id": id })
  }

  log("info", `Added to cart: ${product.name}`, {
    "shop.product_id": id,
    "shop.price": product.price,
  })
  return Response.json({ ok: true })
}
