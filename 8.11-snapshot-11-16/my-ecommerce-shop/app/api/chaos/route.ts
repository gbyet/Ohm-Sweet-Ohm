import { chaos, log, type Chaos } from "@/lib/shop"

export async function GET() {
  return Response.json(chaos)
}

export async function POST(req: Request) {
  const update = (await req.json()) as Partial<Chaos>
  for (const key of Object.keys(update) as (keyof Chaos)[]) {
    if (key in chaos) chaos[key] = Boolean(update[key])
  }
  log("warn", "Chaos settings changed", { ...chaos })
  return Response.json(chaos)
}
