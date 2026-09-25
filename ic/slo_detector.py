"""SLO breach detector — the trigger side of the live loop.

Polls a target service's health endpoint, evaluates it against an SLO
(error_rate + p95 latency), and fires an investigation when the SLO is
breached on N consecutive evaluations. Includes re-arm hysteresis so a
single flap doesn't re-fire the pipeline.

State machine:

    WATCHING -- breach --> BREACH_COUNT(k/N) -- breach --> FIRED
    FIRED    -- healthy --> HEALTHY_COUNT(k/M) -- healthy --> WATCHING

This closes the "how does an investigation start" gap: previously an
incident bundle had to be intake'd manually; now the SLO detector produces
the trigger from live telemetry the way an on-call rotation would.

CLI:
    python -m ic.slo_detector \\
        --target http://localhost:9001/shop/health \\
        --investigate http://localhost:8000/investigate \\
        --incident INC-4478
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from enum import Enum

import httpx


# ---------- SLO thresholds ----------------------------------------------
# checkout-service SLO: <5% 5xx and p95 <= 500ms. If either fails, that's
# a breach. Numbers are picked to match the mock shop-svc's healthy state
# (2% / 210ms healthy vs 68% / 1850ms broken).
DEFAULT_ERROR_RATE_MAX = 0.05
DEFAULT_P95_MS_MAX = 500

# 3 consecutive breach evaluations before we fire; 3 consecutive healthy
# before we re-arm. This is the standard breach-window / re-arm hysteresis
# pattern from Google SRE + AWS CloudWatch alarms.
DEFAULT_BREACH_N = 3
DEFAULT_REARM_N = 3


class State(str, Enum):
    WATCHING = "watching"       # under SLO, counting nothing
    BREACH_COUNT = "breach"     # over SLO, counting up
    FIRED = "fired"             # investigation already triggered
    HEALTHY_COUNT = "healthy"   # back under SLO after fire, counting to re-arm


@dataclass
class SLODetector:
    target_url: str
    investigate_url: str
    incident_id: str
    error_rate_max: float = DEFAULT_ERROR_RATE_MAX
    p95_ms_max: int = DEFAULT_P95_MS_MAX
    breach_n: int = DEFAULT_BREACH_N
    rearm_n: int = DEFAULT_REARM_N

    state: State = State.WATCHING
    _counter: int = 0
    _fires: list[dict] = field(default_factory=list)

    def evaluate(self, sample: dict) -> dict:
        """Consume one telemetry sample; advance the state machine.

        Returns an event dict describing what happened this tick — useful
        for logging and for the console to render a live SLO strip.
        """
        breached = (
            sample.get("error_rate", 0) > self.error_rate_max
            or sample.get("p95_ms", 0) > self.p95_ms_max
        )
        prev = self.state

        if self.state == State.WATCHING:
            if breached:
                self.state = State.BREACH_COUNT
                self._counter = 1
        elif self.state == State.BREACH_COUNT:
            if breached:
                self._counter += 1
                if self._counter >= self.breach_n:
                    self.state = State.FIRED
                    self._counter = 0
                    return self._fire(sample)
            else:
                # single flap under threshold — reset the counter
                self.state = State.WATCHING
                self._counter = 0
        elif self.state == State.FIRED:
            if not breached:
                self.state = State.HEALTHY_COUNT
                self._counter = 1
        elif self.state == State.HEALTHY_COUNT:
            if not breached:
                self._counter += 1
                if self._counter >= self.rearm_n:
                    self.state = State.WATCHING
                    self._counter = 0
            else:
                self.state = State.FIRED
                self._counter = 0

        return {
            "type": "slo_evaluation",
            "prev_state": prev.value,
            "state": self.state.value,
            "counter": self._counter,
            "breached": breached,
            "sample": sample,
        }

    def _fire(self, sample: dict) -> dict:
        """Trigger an investigation via the ingestion API."""
        event = {
            "type": "slo_breach_fired",
            "state": self.state.value,
            "counter": self._counter,
            "breached": True,
            "reason": f"error_rate={sample.get('error_rate'):.2%} > {self.error_rate_max:.0%} "
                      f"OR p95={sample.get('p95_ms')}ms > {self.p95_ms_max}ms, "
                      f"{self.breach_n} consecutive evaluations",
            "incident_id": self.incident_id,
            "sample": sample,
        }
        try:
            r = httpx.post(
                self.investigate_url,
                json={"incident_id": self.incident_id, "probes_enabled": True},
                timeout=30.0,
            )
            event["investigate_status"] = r.status_code
        except httpx.HTTPError as exc:
            event["investigate_error"] = str(exc)
        self._fires.append(event)
        return event


def poll(detector: SLODetector, interval: float = 2.0, max_ticks: int = 0) -> None:
    """Blocking poll loop. max_ticks=0 means loop forever."""
    ticks = 0
    print(f"[slo] watching {detector.target_url} — "
          f"SLO error<{detector.error_rate_max:.0%} p95<{detector.p95_ms_max}ms — "
          f"breach after {detector.breach_n} consecutive, re-arm after {detector.rearm_n}")
    while True:
        try:
            r = httpx.get(detector.target_url, timeout=5.0)
            sample = r.json()
        except Exception as exc:
            print(f"[slo] poll error: {exc}")
            time.sleep(interval)
            continue

        evt = detector.evaluate(sample)
        marker = {
            "watching": ".",
            "breach": "!",
            "fired": "*",
            "healthy": "~",
        }.get(evt["state"], "?")
        line = (f"[slo] {marker} state={evt['state']:>8} "
                f"counter={evt['counter']} "
                f"error_rate={sample.get('error_rate'):.2%} "
                f"p95={sample.get('p95_ms')}ms")
        print(line)
        if evt["type"] == "slo_breach_fired":
            print(f"[slo] >>> FIRED investigation {evt['incident_id']} — {evt['reason']}")

        ticks += 1
        if max_ticks and ticks >= max_ticks:
            break
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="http://localhost:9001/shop/health",
                    help="Health endpoint to poll (returns error_rate + p95_ms)")
    ap.add_argument("--investigate", default="http://localhost:8000/investigate",
                    help="Endpoint to POST when SLO breaches")
    ap.add_argument("--incident", default="INC-4478",
                    help="Incident bundle id to fire on breach")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--error-rate-max", type=float, default=DEFAULT_ERROR_RATE_MAX)
    ap.add_argument("--p95-ms-max", type=int, default=DEFAULT_P95_MS_MAX)
    ap.add_argument("--breach-n", type=int, default=DEFAULT_BREACH_N)
    ap.add_argument("--rearm-n", type=int, default=DEFAULT_REARM_N)
    ap.add_argument("--max-ticks", type=int, default=0,
                    help="Stop after N ticks (0 = forever)")
    args = ap.parse_args()

    det = SLODetector(
        target_url=args.target,
        investigate_url=args.investigate,
        incident_id=args.incident,
        error_rate_max=args.error_rate_max,
        p95_ms_max=args.p95_ms_max,
        breach_n=args.breach_n,
        rearm_n=args.rearm_n,
    )
    poll(det, interval=args.interval, max_ticks=args.max_ticks)


if __name__ == "__main__":
    main()
