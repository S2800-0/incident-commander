"""SLO breach detector — opens an incident from live telemetry, with no human
selecting anything.

A breach must hold for `consecutive` evaluations before an incident opens, so
a single noisy second does not page. After an incident closes the detector
stays disarmed until the SLO has been healthy for `rearm_after` evaluations:
an escalated incident whose fault persists must not re-open every second.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .telemetry import TelemetryStore


@dataclass
class SLO:
    error_rate_max: float = 0.05     # 5xx fraction over the window
    p95_max_ms: float = 600.0
    window_s: float = 5.0
    min_requests: int = 40
    consecutive: int = 3
    rearm_after: int = 5


@dataclass
class Breach:
    signal: str
    value: float
    threshold: float
    window_s: float
    requests: int
    fired_at: float

    def alert(self, service: str) -> dict:
        unit = "%" if self.signal == "http_5xx_rate" else "ms"
        shown = self.value * 100 if unit == "%" else self.value
        limit = self.threshold * 100 if unit == "%" else self.threshold
        return {
            "service": service,
            "environment": "production",
            "severity": "SEV-2",
            "fired_at": datetime.fromtimestamp(self.fired_at, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "signal": self.signal,
            "condition": f"{self.signal} {shown:.1f}{unit} > SLO {limit:g}{unit} "
                         f"over {self.window_s:g}s ({self.requests} requests)",
            "source": "incident-commander/slo-detector",
        }


class Detector:
    def __init__(self, store: TelemetryStore, slo: Optional[SLO] = None) -> None:
        self.store = store
        self.slo = slo or SLO()
        self._streak = 0
        self._healthy_streak = 0
        self.armed = True
        self.last: dict = {}

    def evaluate(self, now: Optional[float] = None) -> Optional[Breach]:
        now = now or time.time()
        s = self.slo
        stats = self.store.request_stats(now - s.window_s, now)
        self.last = {**stats, "evaluated_at": now}
        if stats["requests"] < s.min_requests:
            self._streak = 0
            return None  # not enough traffic to judge

        breach: Optional[Breach] = None
        if stats["error_rate"] is not None and stats["error_rate"] > s.error_rate_max:
            breach = Breach("http_5xx_rate", stats["error_rate"], s.error_rate_max, s.window_s,
                            stats["requests"], now)
        elif stats["p95_ms"] is not None and stats["p95_ms"] > s.p95_max_ms:
            breach = Breach("http_p95_latency", stats["p95_ms"], s.p95_max_ms, s.window_s,
                            stats["requests"], now)

        if breach is None:
            self._streak = 0
            self._healthy_streak += 1
            if not self.armed and self._healthy_streak >= s.rearm_after:
                self.armed = True
            return None

        self._healthy_streak = 0
        self._streak += 1
        if self.armed and self._streak >= s.consecutive:
            self.armed = False
            self._streak = 0
            return breach
        return None
