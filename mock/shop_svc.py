"""checkout-service — the live target Incident Commander observes and acts on.

This is a simulated service, but nothing it reports is typed in by hand:

  * `GET /shop/checkout` serves real HTTP. Every request really awaits its
    dependency hops (inventory-service, orders-db, payment-gateway), really
    returns 200 or 500, and its latency is measured with perf_counter.
  * Every second the service pushes what it actually served to Incident
    Commander as spec-shaped OTLP JSON (`/ingest/otlp/v1/metrics`): request
    counts by version and status class, duration histograms, and
    client-observed dependency latency/outcomes.
  * Faults come from the `/chaos` API. That is the ENVIRONMENT — the outside
    world breaking — and Incident Commander never calls it. The agent only
    sees what the telemetry and the deploy API reveal.
  * `/ops/*` is the actuator surface (the equivalent of a deploy tool / feature
    flag API). Actions really change how subsequent requests are served.

Deliberate blind spot: payment-gateway has no client instrumentation, so a
fault there is invisible to telemetry. That is how the "wrong but defensible
diagnosis, caught by verification" scenario arises honestly.

Run it:
    uvicorn mock.shop_svc:app --port 9001 --log-level warning
Traffic comes from a separate process:
    python -m mock.loadgen
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SERVICE = "checkout-service"
OTLP_URL = os.environ.get("IC_OTLP_METRICS_URL", "http://127.0.0.1:8000/ingest/otlp/v1/metrics")
EXPORT_INTERVAL_S = float(os.environ.get("SHOP_EXPORT_INTERVAL_S", "1.0"))
STABLE_VERSION = "v6.09.0"
PRIOR_STABLE_VERSION = "v6.08.2"

# OTel explicit-bucket histogram bounds, in ms.
BOUNDS_MS = [5, 10, 25, 50, 75, 100, 150, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000]

# Healthy per-hop latency (ms). Real awaited sleeps, with jitter.
BASE_MS = {"inventory-service": 18.0, "orders-db": 12.0, "payment-gateway": 9.0}
APP_MS = 8.0
HOP_ORDER = ("inventory-service", "orders-db", "payment-gateway")
# payment-gateway is not instrumented — its client spans are never exported.
INSTRUMENTED = {"inventory-service", "orders-db"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ============================================================================
# State
# ============================================================================

@dataclass
class Deploy:
    version: str
    kind: str            # "code" | "config" | "rollback" | "revert"
    deployed_at: float
    deployed_by: str
    note: str = ""

    def public(self) -> dict:
        d = asdict(self)
        d["deployed_at"] = _iso(self.deployed_at)
        return d


@dataclass
class Served:
    ts: float
    version: str
    status: int
    total_ms: float
    hops: list  # [(dependency, ms, ok)]


class Service:
    def __init__(self) -> None:
        self._scenario_task: Optional[asyncio.Task] = None
        self._restore_task: Optional[asyncio.Task] = None
        self.export_failures = 0
        self.exports_ok = 0
        self.reset_environment()

    # --- environment --------------------------------------------------------

    def reset_environment(self) -> None:
        now = time.time()
        # A fresh environment: the stable release went out hours ago.
        self.deploys: list[Deploy] = [
            Deploy(PRIOR_STABLE_VERSION, "code", now - 26 * 3600, "release-bot", "previous release"),
            Deploy(STABLE_VERSION, "code", now - 3 * 3600, "release-bot", "current stable release"),
        ]
        self.active_version = STABLE_VERSION
        self.previous_version = PRIOR_STABLE_VERSION
        self.version_error_prob: dict[str, float] = {}
        self.canary: Optional[dict] = None
        self.deps = {d: {"extra_ms": 0.0, "error_prob": 0.0} for d in BASE_MS}
        self.inventory_fallback = False
        self.ops_log: list[dict] = []
        self.scenario: Optional[str] = None
        self.pending: list[Served] = []
        self.recent: deque[Served] = deque(maxlen=20000)
        self._log("environment", "environment reset to healthy stable release", actor="environment")

    def _log(self, kind: str, detail: str, actor: str = "agent", **extra) -> dict:
        entry = {"ts": time.time(), "kind": kind, "detail": detail, "actor": actor, **extra}
        self.ops_log.append(entry)
        del self.ops_log[:-500]
        return entry

    def deploy(self, version: str, kind: str, by: str, note: str) -> None:
        self.previous_version = self.active_version
        self.active_version = version
        self.deploys.append(Deploy(version, kind, time.time(), by, note))
        self._log("deploy", f"{kind} deploy {version} by {by}: {note}", actor="environment")

    # --- request routing ------------------------------------------------------

    def route_version(self, now: float) -> str:
        c = self.canary
        if c is not None:
            if now >= c["expires_at"]:
                self.canary = None
                self._log("canary_expired",
                          f"canary to {c['version']} expired after {c['duration_s']}s — "
                          "traffic returned to active version (envelope enforced by service)",
                          actor="service")
            elif random.random() < c["pct"] / 100.0:
                return c["version"]
        return self.active_version

    def record(self, s: Served) -> None:
        self.pending.append(s)
        self.recent.append(s)

    def drain(self) -> list[Served]:
        batch, self.pending = self.pending, []
        return batch

    # --- deploy-tool view -------------------------------------------------------

    def last_release(self) -> Deploy:
        for d in reversed(self.deploys):
            if d.kind in ("code", "config"):
                return d
        return self.deploys[-1]

    def capabilities(self) -> dict:
        rel = self.last_release()
        cohort = rel.kind == "code" and self.previous_version is not None \
            and rel.version == self.active_version
        return {
            "cohort_routing": cohort,
            "cohort_routing_reason": (
                "per-version routing available for code releases" if cohort else
                "config changes apply to every instance — no per-version cohort to compare"
                if rel.kind == "config" else "active version is not the latest release"),
            "inventory_fallback": True,
        }

    def state(self) -> dict:
        return {
            "service": SERVICE,
            "active_version": self.active_version,
            "previous_version": self.previous_version,
            "last_release": self.last_release().public(),
            "canary": ({**self.canary, "expires_at": _iso(self.canary["expires_at"])}
                       if self.canary else None),
            "inventory_fallback": self.inventory_fallback,
            "capabilities": self.capabilities(),
        }

    def measured_health(self, window_s: float = 10.0) -> dict:
        """What the service actually served recently — for display only."""
        now = time.time()
        rows = [s for s in self.recent if s.ts >= now - window_s]
        n = len(rows)
        errs = sum(1 for s in rows if s.status >= 500)
        lat = sorted(s.total_ms for s in rows)
        p95 = lat[int(0.95 * (n - 1))] if n else 0.0
        return {
            "version": self.active_version,
            "error_rate": round(errs / n, 4) if n else 0.0,
            "p95_ms": int(p95),
            "requests": n,
            "window_s": window_s,
            "since": self.deploys[-1].deployed_at,
            "reason": self.deploys[-1].note,
        }


svc = Service()


# ============================================================================
# OTLP export — what was actually served, every EXPORT_INTERVAL_S
# ============================================================================

def _kv(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _histogram_point(values: list[float], t0: int, t1: int, attrs: list[dict]) -> dict:
    counts = [0] * (len(BOUNDS_MS) + 1)
    for v in values:
        i = 0
        while i < len(BOUNDS_MS) and v > BOUNDS_MS[i]:
            i += 1
        counts[i] += 1
    return {
        "startTimeUnixNano": str(t0), "timeUnixNano": str(t1),
        "count": str(len(values)), "sum": round(sum(values), 3),
        "bucketCounts": [str(c) for c in counts], "explicitBounds": list(BOUNDS_MS),
        "attributes": attrs,
    }


def build_otlp(batch: list[Served], t_start: float, t_end: float) -> dict:
    t0, t1 = int(t_start * 1e9), int(t_end * 1e9)
    req_counts: dict[tuple, int] = {}
    durations: dict[str, list[float]] = {}
    dep_counts: dict[tuple, int] = {}
    dep_durations: dict[str, list[float]] = {}
    for s in batch:
        cls = "5xx" if s.status >= 500 else "2xx"
        req_counts[(s.version, cls)] = req_counts.get((s.version, cls), 0) + 1
        durations.setdefault(s.version, []).append(s.total_ms)
        for dep, ms, ok in s.hops:
            if dep not in INSTRUMENTED:
                continue
            key = (dep, "ok" if ok else "error")
            dep_counts[key] = dep_counts.get(key, 0) + 1
            dep_durations.setdefault(dep, []).append(ms)

    metrics = [
        {"name": "http.server.request.count", "unit": "{request}",
         "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
             {"startTimeUnixNano": str(t0), "timeUnixNano": str(t1), "asInt": str(n),
              "attributes": [_kv("service.version", v), _kv("http.response.status_class", c)]}
             for (v, c), n in sorted(req_counts.items())]}},
        {"name": "http.server.request.duration", "unit": "ms",
         "histogram": {"aggregationTemporality": 1, "dataPoints": [
             _histogram_point(vals, t0, t1, [_kv("service.version", v)])
             for v, vals in sorted(durations.items())]}},
    ]
    if dep_counts:
        metrics.append(
            {"name": "dependency.client.request.count", "unit": "{request}",
             "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
                 {"startTimeUnixNano": str(t0), "timeUnixNano": str(t1), "asInt": str(n),
                  "attributes": [_kv("peer.service", d), _kv("outcome", o)]}
                 for (d, o), n in sorted(dep_counts.items())]}})
        metrics.append(
            {"name": "dependency.client.duration", "unit": "ms",
             "histogram": {"aggregationTemporality": 1, "dataPoints": [
                 _histogram_point(vals, t0, t1, [_kv("peer.service", d)])
                 for d, vals in sorted(dep_durations.items())]}})

    return {"resourceMetrics": [{
        "resource": {"attributes": [
            _kv("service.name", SERVICE),
            _kv("deployment.environment", "production"),
            _kv("service.instance.id", f"{SERVICE}-{os.getpid()}"),
        ]},
        "scopeMetrics": [{"scope": {"name": "checkout-service.manual-instrumentation", "version": "1.0"},
                          "metrics": metrics}],
    }]}


async def _export_loop() -> None:
    t_prev = time.time()
    async with httpx.AsyncClient(timeout=0.8) as client:
        while True:
            await asyncio.sleep(EXPORT_INTERVAL_S)
            t_now = time.time()
            batch = svc.drain()
            if batch:
                try:
                    r = await client.post(OTLP_URL, json=build_otlp(batch, t_prev, t_now))
                    if r.status_code < 300:
                        svc.exports_ok += 1
                    else:
                        svc.export_failures += 1
                except httpx.HTTPError:
                    svc.export_failures += 1  # IC not up yet — telemetry for this tick is lost
            t_prev = t_now


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_export_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="checkout-service (live target)", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ============================================================================
# Serving path
# ============================================================================

async def _hop(dep: str) -> tuple[float, bool]:
    cfg = svc.deps[dep]
    target = max(1.0, random.gauss(BASE_MS[dep], BASE_MS[dep] * 0.15))
    if cfg["extra_ms"]:
        target += max(0.0, random.gauss(cfg["extra_ms"], cfg["extra_ms"] * 0.1))
    t = time.perf_counter()
    await asyncio.sleep(target / 1000.0)
    measured = (time.perf_counter() - t) * 1000.0
    return measured, random.random() >= cfg["error_prob"]


@app.get("/shop/checkout")
async def checkout():
    t = time.perf_counter()
    version = svc.route_version(time.time())
    hops: list = []
    ok = True
    for dep in HOP_ORDER:
        if dep == "inventory-service" and svc.inventory_fallback:
            continue  # served from cache — no call made
        ms, dep_ok = await _hop(dep)
        hops.append((dep, ms, dep_ok))
        if not dep_ok:
            ok = False
            break  # the request fails at the first failing dependency
    if ok:
        await asyncio.sleep(max(1.0, random.gauss(APP_MS, APP_MS * 0.2)) / 1000.0)
        if random.random() < svc.version_error_prob.get(version, 0.0):
            ok = False  # application defect in this version's code path
    status = 200 if ok else 500
    total = (time.perf_counter() - t) * 1000.0
    svc.record(Served(time.time(), version, status, total, hops))
    return JSONResponse({"ok": ok, "version": version}, status_code=status)


# ============================================================================
# Actuator API — what Incident Commander's executor calls
# ============================================================================

class OpContext(BaseModel):
    incident_id: Optional[str] = None
    reason: Optional[str] = None


class SetVersion(OpContext):
    version: str


class Canary(OpContext):
    version: str
    pct: float
    duration_s: float


class Fallback(OpContext):
    enabled: bool


@app.get("/ops/state")
def ops_state() -> dict:
    return svc.state()


@app.get("/ops/deployments")
def ops_deployments(limit: int = 10) -> list[dict]:
    return [d.public() for d in svc.deploys[-limit:]]


@app.get("/ops/log")
def ops_log(limit: int = 50) -> list[dict]:
    return svc.ops_log[-limit:]


@app.post("/ops/rollback")
def ops_rollback(body: OpContext) -> dict:
    if svc.previous_version is None:
        raise HTTPException(409, "no previous version to roll back to")
    before = svc.state()
    target = svc.previous_version
    svc.previous_version = svc.active_version
    svc.active_version = target
    svc.deploys.append(Deploy(target, "rollback", time.time(), "incident-commander",
                              f"rollback for {body.incident_id}"))
    entry = svc._log("rollback", f"rolled back {before['active_version']} → {target}",
                     incident_id=body.incident_id)
    return {"ok": True, "action": "rollback", "before": before, "after": svc.state(), "log": entry}


@app.post("/ops/set-version")
def ops_set_version(body: SetVersion) -> dict:
    before = svc.state()
    svc.previous_version = svc.active_version
    svc.active_version = body.version
    svc.deploys.append(Deploy(body.version, "revert", time.time(), "incident-commander",
                              f"revert for {body.incident_id}: {body.reason or ''}"))
    entry = svc._log("set_version", f"active version {before['active_version']} → {body.version}",
                     incident_id=body.incident_id)
    return {"ok": True, "action": "set_version", "before": before, "after": svc.state(), "log": entry}


@app.post("/ops/canary")
def ops_canary(body: Canary) -> dict:
    # The service enforces its own hard envelope too; policy is checked before
    # the call ever arrives, this is defence in depth.
    if not (0 < body.pct <= 50) or not (0 < body.duration_s <= 60):
        raise HTTPException(422, "canary envelope out of bounds (pct ≤ 50, duration ≤ 60s)")
    now = time.time()
    svc.canary = {"version": body.version, "pct": body.pct, "duration_s": body.duration_s,
                  "started_at": now, "expires_at": now + body.duration_s,
                  "incident_id": body.incident_id}
    entry = svc._log("canary", f"routing {body.pct:g}% of traffic to {body.version} "
                               f"for {body.duration_s:g}s", incident_id=body.incident_id)
    return {"ok": True, "action": "canary", "state": svc.state(), "log": entry}


@app.post("/ops/inventory-fallback")
def ops_inventory_fallback(body: Fallback) -> dict:
    before = svc.state()
    svc.inventory_fallback = body.enabled
    entry = svc._log("inventory_fallback", f"inventory fallback {'enabled' if body.enabled else 'disabled'}",
                     incident_id=body.incident_id)
    return {"ok": True, "action": "inventory_fallback", "before": before, "after": svc.state(), "log": entry}


@app.post("/ops/failover-database")
async def ops_failover_database(body: OpContext) -> dict:
    """Destructive by design: a failover drops every in-flight DB connection for
    20s. This is exactly why policy forbids agents from calling it."""
    svc.deps["orders-db"]["error_prob"] = 1.0

    async def _restore():
        await asyncio.sleep(20)
        svc.deps["orders-db"]["error_prob"] = 0.0
        svc._log("failover_complete", "orders-db failover complete", actor="service")

    svc._restore_task = asyncio.create_task(_restore())
    entry = svc._log("failover_database", "orders-db failover started — all DB calls failing for 20s",
                     incident_id=body.incident_id)
    return {"ok": True, "action": "failover_database", "log": entry}


# ============================================================================
# Chaos API — the ENVIRONMENT. Incident Commander never calls this.
# ============================================================================

async def _bad_deploy() -> None:
    # A coincidental inventory latency blip first (the trap), then a real code defect.
    svc.deps["inventory-service"]["extra_ms"] = 160.0
    await asyncio.sleep(5)
    svc.deps["inventory-service"]["extra_ms"] = 0.0
    await asyncio.sleep(1)
    svc.deploy("v6.10.0", "code", "ci-pipeline", "checkout pricing refactor")
    svc.version_error_prob["v6.10.0"] = 0.30


async def _config_regression_hidden_dependency() -> None:
    # A harmless config release lands; one second later the uninstrumented
    # payment-gateway starts failing. Telemetry cannot see the real cause.
    svc.deploy("v6.09.1", "config", "ci-pipeline", "raise payment client retry budget")
    await asyncio.sleep(1)
    svc.deps["payment-gateway"]["error_prob"] = 0.22


async def _correlated_dependency_degradation() -> None:
    # Both instrumented dependencies degrade at the same instant (a shared
    # network segment) — telemetry cannot separate which one is the cause.
    for dep in ("orders-db", "inventory-service"):
        svc.deps[dep]["extra_ms"] = 220.0
        svc.deps[dep]["error_prob"] = 0.08


async def _database_saturation() -> None:
    svc.deps["orders-db"]["extra_ms"] = 380.0
    svc.deps["orders-db"]["error_prob"] = 0.18


SCENARIOS = {
    "bad_deploy": _bad_deploy,
    "config_regression_hidden_dependency": _config_regression_hidden_dependency,
    "correlated_dependency_degradation": _correlated_dependency_degradation,
    "database_saturation": _database_saturation,
}


def _cancel_scenario() -> None:
    for t in (svc._scenario_task, svc._restore_task):
        if t and not t.done():
            t.cancel()


@app.post("/chaos/scenario/{name}")
async def chaos_scenario(name: str) -> dict:
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario; choose one of {sorted(SCENARIOS)}")
    _cancel_scenario()
    svc.scenario = name
    svc._log("chaos", f"scenario injected: {name}", actor="environment")
    svc._scenario_task = asyncio.create_task(SCENARIOS[name]())
    return {"ok": True, "scenario": name, "injected_at": time.time()}


@app.post("/chaos/clear")
def chaos_clear() -> dict:
    _cancel_scenario()
    svc.reset_environment()
    return {"ok": True, "cleared_at": time.time()}


@app.get("/chaos/state")
def chaos_state() -> dict:
    """Ground truth for the experiment runner and the console's environment
    panel. Never read by Incident Commander."""
    return {"scenario": svc.scenario, "deps": svc.deps, "version_error_prob": svc.version_error_prob,
            "scenarios": sorted(SCENARIOS), "exports_ok": svc.exports_ok,
            "export_failures": svc.export_failures}


# ============================================================================
# Legacy endpoints — kept for the replay-mode console panel and the replay
# orchestrator's rollback executor (ic/orchestrator.py).
# ============================================================================

@app.get("/shop/health")
def health() -> dict:
    return svc.measured_health()


@app.get("/shop/events")
def events(limit: int = 20) -> list[dict]:
    return [{"ts": e["ts"], "kind": e["kind"], "detail": e["detail"]} for e in svc.ops_log[-limit:]]


class RollbackAction(BaseModel):
    reason: Optional[str] = None
    incident_id: Optional[str] = None


@app.post("/shop/rollback")
def rollback(body: RollbackAction) -> dict:
    out = ops_rollback(OpContext(incident_id=body.incident_id, reason=body.reason))
    return {"ok": True, "acted": "rollback", "state": svc.measured_health(),
            "context": body.model_dump(), "after": out["after"]}


@app.post("/shop/reset")
async def reset() -> dict:
    _cancel_scenario()
    svc.reset_environment()
    return {"ok": True, "state": svc.measured_health()}
