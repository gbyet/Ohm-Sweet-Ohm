import { PRODUCTS, sleep, step } from "@/lib/shop"

export async function GET() {
  const products = await step("db.products.list", async (span) => {
    await sleep(50 + Math.random() * 150)
    span.setAttribute("shop.product_count", PRODUCTS.length)
    return PRODUCTS
  })
  return Response.json(products)
}
