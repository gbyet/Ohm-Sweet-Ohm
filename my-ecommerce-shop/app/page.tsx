"use client"

import * as React from "react"
import { toast } from "sonner"
import { Eye, ShoppingBag, ShoppingCart, Trash2, Zap } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import type { Chaos, Product } from "@/lib/shop"

type Activity = {
  id: number
  method: string
  path: string
  status: number
  ms: number
}

const CHAOS_OPTIONS: { key: keyof Chaos; label: string; hint: string }[] = [
  {
    key: "flakyCart",
    label: "Flaky cart",
    hint: "30% of “Add to cart” calls fail",
  },
  {
    key: "slowCheckout",
    label: "Slow checkout",
    hint: "Payment takes 2–4 seconds",
  },
  {
    key: "paymentOutage",
    label: "Payment outage",
    hint: "Every checkout fails",
  },
  {
    key: "brokenMystery",
    label: "Broken Mystery Box",
    hint: "Its product page crashes",
  },
]

export default function ShopPage() {
  const [products, setProducts] = React.useState<Product[]>([])
  const [cart, setCart] = React.useState<string[]>([])
  const [chaos, setChaos] = React.useState<Chaos | null>(null)
  const [activity, setActivity] = React.useState<Activity[]>([])
  const [cartOpen, setCartOpen] = React.useState(false)
  const [busy, setBusy] = React.useState<string | null>(null)

  // Every API call goes through here so it shows up in the "Live requests" panel.
  const call = React.useCallback(
    async (method: string, path: string, body?: unknown) => {
      const start = performance.now()
      const res = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      const ms = Math.round(performance.now() - start)
      setActivity((a) =>
        [
          {
            id: Date.now() + Math.random(),
            method,
            path,
            status: res.status,
            ms,
          },
          ...a,
        ].slice(0, 12)
      )
      const data = await res.json().catch(() => ({}))
      return { ok: res.ok, data }
    },
    []
  )

  // Load products and chaos settings once on page load.
  React.useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- state is set after the fetch resolves
    call("GET", "/api/products").then((r) => r.ok && setProducts(r.data))
    call("GET", "/api/chaos").then((r) => r.ok && setChaos(r.data))
  }, [call])

  const byId = (id: string) => products.find((p) => p.id === id)
  const total = cart.reduce((sum, id) => sum + (byId(id)?.price ?? 0), 0)

  async function view(p: Product) {
    const r = await call("GET", `/api/products/${p.id}`)
    if (r.ok)
      toast(`${p.emoji} ${p.name}`, {
        description: `$${p.price} · ${p.stock} in stock`,
      })
    else toast.error("Product page crashed", { description: r.data.error })
  }

  async function addToCart(p: Product) {
    setBusy(p.id)
    const r = await call("POST", "/api/cart", { id: p.id })
    setBusy(null)
    if (r.ok) {
      setCart((c) => [...c, p.id])
      toast.success(`Added ${p.name}`)
    } else toast.error("Couldn't add to cart", { description: r.data.error })
  }

  async function checkout() {
    setBusy("checkout")
    const r = await call("POST", "/api/checkout", { items: cart })
    setBusy(null)
    if (r.ok) {
      setCart([])
      setCartOpen(false)
      toast.success(`Order ${r.data.orderId} placed`, {
        description: `Total $${r.data.total}`,
      })
    } else toast.error("Checkout failed", { description: r.data.error })
  }

  async function toggleChaos(key: keyof Chaos, value: boolean) {
    const r = await call("POST", "/api/chaos", { [key]: value })
    if (r.ok) setChaos(r.data)
  }

  return (
    <div className="min-h-svh bg-background">
      <header className="sticky top-0 z-10 border-b bg-background/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-6">
          <div className="flex items-center gap-2 font-semibold">
            <ShoppingBag className="size-5" />
            Glitchy Shop
            <Badge variant="outline" className="ml-1 font-mono text-[10px]">
              demo
            </Badge>
          </div>
          <Button variant="outline" onClick={() => setCartOpen(true)}>
            <ShoppingCart />
            Cart
            {cart.length > 0 && <Badge className="ml-1">{cart.length}</Badge>}
          </Button>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl gap-6 px-6 py-8 lg:grid-cols-[1fr_320px]">
        <section>
          <div className="mb-6">
            <h1 className="text-2xl font-semibold tracking-tight">Products</h1>
            <p className="text-sm text-muted-foreground">
              Click around. Every request is traced and sent to Dash0.
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {products.map((p) => (
              <Card key={p.id} className="overflow-hidden pt-0">
                <div
                  className={`flex h-36 items-center justify-center text-6xl ${p.color}`}
                >
                  {p.emoji}
                </div>
                <CardHeader>
                  <CardTitle>{p.name}</CardTitle>
                  <CardDescription className="flex items-center gap-2">
                    <span className="font-mono text-foreground">
                      ${p.price}
                    </span>
                    {p.stock <= 4 ? (
                      <Badge className="bg-amber-500/15 text-amber-700 dark:text-amber-400">
                        Only {p.stock} left
                      </Badge>
                    ) : (
                      <Badge className="bg-emerald-500/15 text-emerald-700 dark:text-emerald-400">
                        In stock
                      </Badge>
                    )}
                  </CardDescription>
                </CardHeader>
                <CardFooter className="gap-2">
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => view(p)}
                    aria-label="View details"
                  >
                    <Eye />
                  </Button>
                  <Button
                    className="flex-1"
                    disabled={busy === p.id}
                    onClick={() => addToCart(p)}
                  >
                    {busy === p.id ? "Adding…" : "Add to cart"}
                  </Button>
                </CardFooter>
              </Card>
            ))}
          </div>
        </section>

        <aside className="flex flex-col gap-6">
          <Card className="border-orange-500/40">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Zap className="size-4 text-orange-500" />
                Chaos panel
              </CardTitle>
              <CardDescription>
                Break things on purpose and watch Dash0.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {CHAOS_OPTIONS.map((o) => (
                <div
                  key={o.key}
                  className="flex items-center justify-between gap-4"
                >
                  <Label
                    htmlFor={o.key}
                    className="flex flex-col items-start gap-0.5"
                  >
                    <span>{o.label}</span>
                    <span className="text-xs font-normal text-muted-foreground">
                      {o.hint}
                    </span>
                  </Label>
                  <Switch
                    id={o.key}
                    className="data-checked:bg-orange-500"
                    checked={chaos?.[o.key] ?? false}
                    disabled={!chaos}
                    onCheckedChange={(v) => toggleChaos(o.key, v)}
                  />
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Live requests</CardTitle>
              <CardDescription>What the server just answered.</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-1.5 font-mono text-xs">
              {activity.length === 0 && (
                <p className="text-muted-foreground">No requests yet.</p>
              )}
              {activity.map((a) => (
                <div key={a.id} className="flex items-center gap-2">
                  <span
                    className={`w-9 rounded px-1 text-center ${
                      a.status < 400
                        ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                        : "bg-red-500/15 text-red-700 dark:text-red-400"
                    }`}
                  >
                    {a.status}
                  </span>
                  <span className="w-9 text-muted-foreground">{a.method}</span>
                  <span className="flex-1 truncate">{a.path}</span>
                  <span
                    className={
                      a.ms > 1000
                        ? "text-amber-600 dark:text-amber-400"
                        : "text-muted-foreground"
                    }
                  >
                    {a.ms}ms
                  </span>
                </div>
              ))}
            </CardContent>
          </Card>
        </aside>
      </main>

      <Sheet open={cartOpen} onOpenChange={setCartOpen}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>Your cart</SheetTitle>
            <SheetDescription>{cart.length} item(s)</SheetDescription>
          </SheetHeader>
          <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-4">
            {cart.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Your cart is empty.
              </p>
            )}
            {cart.map((id, i) => {
              const p = byId(id)
              if (!p) return null
              return (
                <div key={i} className="flex items-center gap-3">
                  <div
                    className={`flex size-10 items-center justify-center rounded-md text-xl ${p.color}`}
                  >
                    {p.emoji}
                  </div>
                  <div className="flex-1">
                    <div className="text-sm font-medium">{p.name}</div>
                    <div className="font-mono text-xs text-muted-foreground">
                      ${p.price}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Remove"
                    onClick={() => setCart((c) => c.filter((_, j) => j !== i))}
                  >
                    <Trash2 />
                  </Button>
                </div>
              )
            })}
          </div>
          <SheetFooter>
            <Separator />
            <div className="flex items-center justify-between text-sm">
              <span>Total</span>
              <span className="font-mono font-semibold">${total}</span>
            </div>
            <Button
              disabled={cart.length === 0 || busy === "checkout"}
              onClick={checkout}
            >
              {busy === "checkout" ? "Processing payment…" : "Checkout"}
            </Button>
          </SheetFooter>
        </SheetContent>
      </Sheet>
    </div>
  )
}
