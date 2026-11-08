import { trace } from "@opentelemetry/api"
import { PRODUCTS, chaos, fail, log, sleep, step } from "@/lib/shop"

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params
  trace.getActiveSpan()?.setAttribute("shop.product_id", id)

  try {
    const product = await step("db.products.get", async (span) => {
      span.setAttribute("shop.product_id", id)
      await sleep(30 + Math.random() * 100)
      if (id === "mystery" && chaos.brokenMystery) {
        throw new TypeError(
          "Cannot read properties of undefined (reading 'contents')"
        )
      }
      return PRODUCTS.find((p) => p.id === id)
    })
    if (!product)
      return fail(404, `Product ${id} not found`, { "shop.product_id": id })
    log("info", `Product viewed: ${product.name}`, { "shop.product_id": id })
    return Response.json(product)
  } catch (err) {
    return fail(500, `Product page crashed: ${(err as Error).message}`, {
      "shop.product_id": id,
    })
  }
}
