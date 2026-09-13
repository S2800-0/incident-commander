"""Mock target service — the thing Incident Commander actually acts on.

A tiny FastAPI service simulating `checkout-service` in production. It has
state (`version`, `error_rate`, `p95_ms`), reacts to a real HTTP POST from
the orchestrator's rollback executor, and streams its state so the console
can show a live before/after picture.

This is NOT a production system — it's a plausible target for the demo. The
point is that a judge can see: (1) the console recommend a rollback, (2) the
console fire a real HTTP call to this service, (3) this service's state
change, (4) the console's own live verification confirm recovery. Nothing is
faked; the HTTP calls really flow.

Run it:
    uvicorn mock.shop_svc:app --port 9001 --log-level warning
or via docker compose:
    docker compose up -d shop-svc
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="checkout-service (mock)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ============================================================================
# State — in-memory. Two profiles: broken (v6.10.0) and healthy (v6.09.0).
# ============================================================================

class ServiceState(BaseModel):
    version: str
    error_rate: float          # 0..1, fraction of requests returning 5xx
    p95_ms: int                # p95 request latency
    since: float               # unix time this state was entered
    reason: str                # what caused the current state

class EventLine(BaseModel):
    ts: float
    kind: str                  # "deploy" | "rollback" | "reset"
    detail: str


_BROKEN = {
    "version":    "v6.10.0",
    "error_rate": 0.68,
    "p95_ms":     1850,
    "reason":     "deploy v6.10.0 at 15:00 introduced a hot query pattern (broken)",
}
_HEALTHY = {
    "version":    "v6.09.0",
    "error_rate": 0.02,
    "p95_ms":     210,
    "reason":     "rolled back to v6.09.0 — pre-incident baseline",
}


class _Store:
    def __init__(self) -> None:
        self.state: ServiceState = ServiceState(since=time.time(), **_BROKEN)
        self.events: list[EventLine] = [EventLine(
            ts=self.state.since, kind="deploy",
            detail=f"deploy {self.state.version} — 5xx rate climbed to {int(self.state.error_rate*100)}%",
        )]

    def rollback(self) -> None:
        now = time.time()
        self.state = ServiceState(since=now, **_HEALTHY)
        self.events.append(EventLine(
            ts=now, kind="rollback",
            detail=f"rollback to {self.state.version} executed — 5xx rate now {int(self.state.error_rate*100)}%",
        ))

    def reset(self) -> None:
        now = time.time()
        self.state = ServiceState(since=now, **_BROKEN)
        self.events.append(EventLine(
            ts=now, kind="reset",
            detail=f"reset to {self.state.version} — 5xx rate back to {int(self.state.error_rate*100)}% (demo replay)",
        ))


_store = _Store()


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/shop/health")
def health() -> ServiceState:
    """Live state — pollable by the console."""
    return _store.state


@app.get("/shop/events")
def events(limit: int = 20) -> list[EventLine]:
    return _store.events[-max(1, min(limit, len(_store.events))):]


class RollbackAction(BaseModel):
    reason: Optional[str] = None
    incident_id: Optional[str] = None


@app.post("/shop/rollback")
def rollback(body: RollbackAction) -> dict:
    """Real HTTP POST target for Incident Commander's rollback executor.
    Flips the service state to a healthy baseline and records the event."""
    _store.rollback()
    return {
        "ok":       True,
        "acted":    "rollback",
        "state":    _store.state.model_dump(),
        "context":  body.model_dump() if body else {},
    }


@app.post("/shop/reset")
def reset() -> dict:
    """Reset to broken state for a demo re-run."""
    _store.reset()
    return {"ok": True, "state": _store.state.model_dump()}
