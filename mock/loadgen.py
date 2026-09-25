"""Load generator — real HTTP traffic against checkout-service.

A separate process, so the target's telemetry reflects requests that actually
crossed the network stack. Open-loop: requests fire on schedule whether or not
earlier ones have returned, like real users do (bounded to protect the host).

    python -m mock.loadgen                      # 60 rps against :9001
    python -m mock.loadgen --rps 40 --url http://localhost:9001
"""
from __future__ import annotations

import argparse
import asyncio
import time

import httpx


async def run(url: str, rps: float, max_inflight: int) -> None:
    sem = asyncio.Semaphore(max_inflight)
    stats = {"sent": 0, "ok": 0, "5xx": 0, "failed": 0, "dropped": 0}
    limits = httpx.Limits(max_connections=max_inflight, max_keepalive_connections=max_inflight)

    async with httpx.AsyncClient(timeout=5.0, limits=limits) as client:
        async def one() -> None:
            try:
                r = await client.get(f"{url}/shop/checkout")
                stats["ok" if r.status_code < 500 else "5xx"] += 1
            except httpx.HTTPError:
                stats["failed"] += 1  # target down or timed out
            finally:
                sem.release()

        # Tick-based scheduling: wake every TICK_S and fire however many requests
        # are due by elapsed time. Sleeping once per request does not work on
        # Windows — the ~15.6 ms timer resolution rounds a 16.7 ms sleep up to
        # two ticks and silently caps the generator near 30 rps.
        TICK_S = 0.05
        start = time.perf_counter()
        scheduled = 0
        last_report = time.time()
        # asyncio holds only weak references to tasks: an unreferenced in-flight
        # request can be garbage-collected, its `finally` never runs, and its
        # semaphore permit leaks until the generator stalls. Keep them alive.
        inflight: set[asyncio.Task] = set()
        while True:
            await asyncio.sleep(TICK_S)
            due = int((time.perf_counter() - start) * rps) - scheduled
            if due > rps:  # process was paused for over a second: skip the backlog
                stats["dropped"] += due - int(rps)
                scheduled += due - int(rps)
                due = int(rps)
            for _ in range(due):
                scheduled += 1
                if sem.locked():
                    stats["dropped"] += 1  # host saturated — shed rather than queue unboundedly
                    continue
                await sem.acquire()
                stats["sent"] += 1
                task = asyncio.create_task(one())
                inflight.add(task)
                task.add_done_callback(inflight.discard)

            if time.time() - last_report >= 10:
                last_report = time.time()
                print(f"[loadgen] sent={stats['sent']} ok={stats['ok']} 5xx={stats['5xx']} "
                      f"failed={stats['failed']} dropped={stats['dropped']}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:9001")
    ap.add_argument("--rps", type=float, default=60.0)
    ap.add_argument("--max-inflight", type=int, default=300)
    args = ap.parse_args()
    print(f"[loadgen] {args.rps:g} rps → {args.url}/shop/checkout", flush=True)
    asyncio.run(run(args.url, args.rps, args.max_inflight))


if __name__ == "__main__":
    main()
