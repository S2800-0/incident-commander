"""Post-remediation verification from FRESH telemetry only.

The window starts after the action has settled, so no request served before
the action can count toward "recovered". Nothing here reads fixtures, ground
truth, or the target's internal state.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..models import EvidenceItem
from .detector import SLO
from .probes import iso
from .telemetry import TelemetryStore

SETTLE_S = 2.0
WINDOW_S = 6.0
MIN_REQUESTS = 60
RECOVERED_ERROR_RATE = 0.02   # stricter than the 5% paging threshold: recovered means healthy, not just below the alarm


@dataclass
class Verification:
    recovered: bool
    stats: dict
    evidence: EvidenceItem


def verify_recovery(store: TelemetryStore, incident_id: str, action_at: float, label: str,
                    slo: SLO) -> Verification:
    w0, w1 = action_at + SETTLE_S, action_at + SETTLE_S + WINDOW_S
    wait = w1 - time.time()
    if wait > 0:
        time.sleep(wait)
    covered = store.wait_until_covered(w1, timeout_s=5.0)

    before = store.request_stats(action_at - WINDOW_S, action_at)
    after = store.request_stats(w0, w1)
    checks = {
        "enough_traffic": after["requests"] >= MIN_REQUESTS,
        "error_rate_ok": after["error_rate"] is not None and after["error_rate"] <= RECOVERED_ERROR_RATE,
        "latency_ok": after["p95_ms"] is not None and after["p95_ms"] <= slo.p95_max_ms,
    }
    recovered = all(checks.values())
    stats = {"before_window": [iso(action_at - WINDOW_S), iso(action_at)], "before": before,
             "after_window": [iso(w0), iso(w1)], "after": after,
             "criteria": {"min_requests": MIN_REQUESTS, "max_error_rate": RECOVERED_ERROR_RATE,
                          "max_p95_ms": slo.p95_max_ms},
             "checks": checks, "recovered": recovered, "telemetry_window_complete": covered}
    item = EvidenceItem(
        source_type="verification",
        source_uri=f"verification://{incident_id}/{label}",
        retrieved_at=iso(time.time()), measures=["http_5xx_rate", "http_p95_latency"],
        payload={"kind": "fresh_telemetry_recovery_check", "source": "live OTLP telemetry", **stats})
    return Verification(recovered, stats, item)
