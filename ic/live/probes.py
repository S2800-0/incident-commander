"""Probe execution — each probe is a real query (or a real bounded traffic
split) whose statistics map to an outcome label.

A probe returns outcome=None when its data is too thin to decide; the
investigator then records it as inconclusive and does not update beliefs.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..models import EvidenceItem
from .catalog import CANARY_DURATION_S, CANARY_PCT, ProbeSpec
from .target import TargetClient
from .telemetry import TelemetryStore

CURRENT_WINDOW_S = 4.0
BASELINE_WINDOW = (45.0, 15.0)  # seconds before the alert: [fired − 45s, fired − 15s]

# dependency degradation thresholds
DEP_MIN_CALLS = 20
DEP_ERROR_RATE = 0.05
DEP_LATENCY_RATIO = 2.0
DEP_LATENCY_MIN_DELTA_MS = 50.0

# cohort comparison thresholds
COHORT_MIN_REQUESTS = 20
COHORT_MIN_DIFF = 0.05
COHORT_Z = 3.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ProbeOutput:
    outcome: Optional[str]
    stats: dict
    evidence: EvidenceItem
    duration_ms: float
    actions: list[dict] = field(default_factory=list)   # state changes the probe made


def _degraded(stats: dict, base: dict, min_calls: int) -> tuple[Optional[bool], list[str]]:
    """Is this window degraded vs baseline? None when the window is too thin to judge."""
    if stats["calls"] < min_calls:
        return None, [f"only {stats['calls']} calls (< {min_calls})"]
    reasons: list[str] = []
    err = stats["error_rate"] or 0.0
    errors_high = err >= DEP_ERROR_RATE
    if errors_high:
        reasons.append(f"error rate {err:.1%} ≥ {DEP_ERROR_RATE:.0%}")
    latency_high = False
    if base["p95_ms"] and stats["p95_ms"] is not None:
        limit = max(DEP_LATENCY_RATIO * base["p95_ms"], base["p95_ms"] + DEP_LATENCY_MIN_DELTA_MS)
        latency_high = stats["p95_ms"] >= limit
        if latency_high:
            reasons.append(f"p95 {stats['p95_ms']:.0f}ms ≥ {limit:.0f}ms (baseline {base['p95_ms']:.0f}ms)")
    else:
        reasons.append("no latency baseline — judged on error rate only")
    return errors_high or latency_high, reasons


def dependency_probe(spec: ProbeSpec, store: TelemetryStore, incident_id: str,
                     fired_at: float) -> ProbeOutput:
    """Degraded means SUSTAINED degradation: both halves of the current window
    must be degraded. A transient that has already ended (e.g. the tail of a
    latency blip) touches only the older half and reads as healthy. Without
    this, a coincidental blip vetoed a correctly-confirmed deploy diagnosis in
    live testing (LIVE-0012, LIVE-0052)."""
    t = time.perf_counter()
    # "now" is the newest interval telemetry has fully delivered, not wall time —
    # otherwise the current window's tail would still be in flight.
    now = store.max_point_t or time.time()
    b0, b1 = fired_at - BASELINE_WINDOW[0], fired_at - BASELINE_WINDOW[1]
    base = store.dependency_stats(spec.dependency, b0, b1)
    mid = now - CURRENT_WINDOW_S / 2
    cur = store.dependency_stats(spec.dependency, now - CURRENT_WINDOW_S, now)
    older = store.dependency_stats(spec.dependency, now - CURRENT_WINDOW_S, mid)
    recent = store.dependency_stats(spec.dependency, mid, now)

    half_min = DEP_MIN_CALLS // 2
    older_bad, older_why = _degraded(older, base, half_min)
    recent_bad, recent_why = _degraded(recent, base, half_min)
    outcome: Optional[str] = None
    if older_bad is not None and recent_bad is not None:
        outcome = "dependency_degraded" if (older_bad and recent_bad) else "dependency_healthy"
    reasons = [f"older half: {'degraded' if older_bad else 'healthy' if older_bad is False else 'n/a'}"
               + (f" ({'; '.join(older_why)})" if older_why else ""),
               f"recent half: {'degraded' if recent_bad else 'healthy' if recent_bad is False else 'n/a'}"
               + (f" ({'; '.join(recent_why)})" if recent_why else "")]
    if older_bad and recent_bad is False:
        reasons.append("transient: degradation already ended — not sustained")
    stats = {"dependency": spec.dependency, "current": cur, "older_half": older, "recent_half": recent,
             "baseline": base, "rule": "degraded only if both halves of the window are degraded",
             "current_window": [iso(now - CURRENT_WINDOW_S), iso(now)],
             "baseline_window": [iso(b0), iso(b1)], "findings": reasons}
    item = EvidenceItem(
        source_type="probe_result",
        source_uri=f"otlp-query://{spec.probe_id}/{incident_id}",
        retrieved_at=iso(now), measures=[spec.observable], probe_id=spec.probe_id,
        payload={"query": "dependency.client.{request.count,duration} by peer.service",
                 "outcome": outcome, **stats})
    return ProbeOutput(outcome, stats, item, (time.perf_counter() - t) * 1000)


def two_proportion_z(e1: int, n1: int, e2: int, n2: int) -> float:
    p = (e1 + e2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) if 0 < p < 1 else 0.0
    diff = e1 / n1 - e2 / n2
    if se == 0:
        return math.inf if diff > 0 else 0.0
    return diff / se


def cohort_probe(spec: ProbeSpec, store: TelemetryStore, target: TargetClient, incident_id: str,
                 active_version: str, previous_version: str) -> ProbeOutput:
    """Interventional: split real traffic, measure real cohort outcomes. The
    service enforces the canary expiry itself, so the split reverts even if
    this process dies mid-probe."""
    t = time.perf_counter()
    resp = target.canary(incident_id, previous_version, CANARY_PCT, CANARY_DURATION_S)
    started = time.time()
    ended = started + CANARY_DURATION_S
    time.sleep(CANARY_DURATION_S)
    covered = store.wait_until_covered(ended + 0.2, timeout_s=5.0)
    after = target.state()

    rows = store.errors_by_version(started + 0.5, ended + 0.2)
    new, old = rows.get(active_version, {"requests": 0, "errors": 0}), \
        rows.get(previous_version, {"requests": 0, "errors": 0})
    outcome: Optional[str] = None
    z = None
    if new["requests"] >= COHORT_MIN_REQUESTS and old["requests"] >= COHORT_MIN_REQUESTS:
        z = two_proportion_z(new["errors"], new["requests"], old["errors"], old["requests"])
        diff = new["errors"] / new["requests"] - old["errors"] / old["requests"]
        worse = diff >= COHORT_MIN_DIFF and z >= COHORT_Z
        outcome = "new_version_worse" if worse else "versions_equal"
    stats = {"active_version": active_version, "previous_version": previous_version,
             "cohorts": {active_version: new, previous_version: old},
             "error_rate_new": (new["errors"] / new["requests"]) if new["requests"] else None,
             "error_rate_previous": (old["errors"] / old["requests"]) if old["requests"] else None,
             "z": (None if z is None else (round(z, 2) if math.isfinite(z) else "inf")),
             "canary": {"pct": CANARY_PCT, "duration_s": CANARY_DURATION_S,
                        "window": [iso(started), iso(ended)]},
             "canary_cleared_after_probe": after.get("canary") is None,
             "telemetry_window_complete": covered}
    item = EvidenceItem(
        source_type="probe_result",
        source_uri=f"otlp-query://{spec.probe_id}/{incident_id}",
        retrieved_at=iso(time.time()), measures=[spec.observable], probe_id=spec.probe_id,
        payload={"query": "http.server.request.count by service.version, status_class during canary",
                 "outcome": outcome, **stats, "canary_response": resp["body"]})
    actions = [{"action": "canary", "version": previous_version, "pct": CANARY_PCT,
                "duration_s": CANARY_DURATION_S, "started_at": started,
                "auto_reverted": stats["canary_cleared_after_probe"]}]
    return ProbeOutput(outcome, stats, item, (time.perf_counter() - t) * 1000, actions)
