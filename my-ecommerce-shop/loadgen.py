#!/usr/bin/env python3
"""
Load generator for the Glitchy Shop: simulates shoppers so the dashboards always have data.

Each shopper loops: browse products -> maybe view one -> add 1-3 items to cart -> maybe check out.

Usage:
    python3 loadgen.py                 # 3 shoppers against http://localhost:3000
    python3 loadgen.py --shoppers 10   # more traffic
    python3 loadgen.py --url http://localhost:3000

Stop with Ctrl+C.
"""

import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request

PRODUCTS = ["sneakers", "headphones", "backpack", "watch", "camera", "mystery"]

lock = threading.Lock()
stats = {"requests": 0, "errors": 0, "orders": 0}


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            ok, payload = True, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError:
        ok, payload = False, {}
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        ok, payload = False, {}
    with lock:
        stats["requests"] += 1
        stats["errors"] += 0 if ok else 1
    return ok, payload


def shopper(base, stop):
    while not stop.is_set():
        call(base, "GET", "/api/products")
        if random.random() < 0.5:
            call(base, "GET", f"/api/products/{random.choice(PRODUCTS)}")

        cart = []
        for _ in range(random.randint(1, 3)):
            item = random.choice(PRODUCTS)
            ok, _ = call(base, "POST", "/api/cart", {"id": item})
            if ok:
                cart.append(item)
            time.sleep(random.uniform(0.2, 0.8))

        if cart and random.random() < 0.7:
            ok, _ = call(base, "POST", "/api/checkout", {"items": cart})
            if ok:
                with lock:
                    stats["orders"] += 1

        stop.wait(random.uniform(1.0, 3.0))  # pause before the next visit


def main():
    ap = argparse.ArgumentParser(description="Simulate shoppers on the Glitchy Shop.")
    ap.add_argument("--url", default="http://localhost:3000")
    ap.add_argument("--shoppers", type=int, default=3)
    args = ap.parse_args()

    stop = threading.Event()
    for _ in range(args.shoppers):
        threading.Thread(target=shopper, args=(args.url, stop), daemon=True).start()

    print(f"Simulating {args.shoppers} shoppers on {args.url}. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(5)
            with lock:
                print(
                    f"requests: {stats['requests']:>5}   errors: {stats['errors']:>4}   orders: {stats['orders']:>4}",
                    flush=True,
                )
    except KeyboardInterrupt:
        stop.set()
        print("\nStopped.")


if __name__ == "__main__":
    main()
