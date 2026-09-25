"""State-machine tests for the SLO detector — no network required."""
from __future__ import annotations

from unittest.mock import patch

from ic.slo_detector import SLODetector, State


def _det() -> SLODetector:
    return SLODetector(
        target_url="http://x/health",
        investigate_url="http://x/investigate",
        incident_id="INC-4478",
        breach_n=3,
        rearm_n=3,
    )


BROKEN = {"error_rate": 0.68, "p95_ms": 1850}
HEALTHY = {"error_rate": 0.02, "p95_ms": 210}


def test_healthy_stays_watching():
    d = _det()
    for _ in range(5):
        d.evaluate(HEALTHY)
    assert d.state == State.WATCHING


def test_single_flap_does_not_fire():
    d = _det()
    d.evaluate(BROKEN)
    d.evaluate(HEALTHY)  # counter resets
    d.evaluate(BROKEN)
    d.evaluate(HEALTHY)
    assert d.state == State.WATCHING
    assert d._fires == []


def test_three_consecutive_breaches_fires():
    d = _det()
    with patch("httpx.post") as p:
        p.return_value.status_code = 200
        d.evaluate(BROKEN)
        d.evaluate(BROKEN)
        d.evaluate(BROKEN)
    assert d.state == State.FIRED
    assert len(d._fires) == 1
    assert d._fires[0]["incident_id"] == "INC-4478"


def test_re_arm_requires_three_healthy():
    d = _det()
    with patch("httpx.post") as p:
        p.return_value.status_code = 200
        for _ in range(3):
            d.evaluate(BROKEN)
        assert d.state == State.FIRED

        d.evaluate(HEALTHY)  # 1/3
        assert d.state == State.HEALTHY_COUNT
        d.evaluate(HEALTHY)  # 2/3
        assert d.state == State.HEALTHY_COUNT
        d.evaluate(HEALTHY)  # 3/3 → re-armed
        assert d.state == State.WATCHING


def test_flap_during_healthy_count_returns_to_fired():
    d = _det()
    with patch("httpx.post") as p:
        p.return_value.status_code = 200
        for _ in range(3):
            d.evaluate(BROKEN)
        d.evaluate(HEALTHY)   # HEALTHY_COUNT 1
        d.evaluate(BROKEN)    # → back to FIRED (no second fire)
    assert d.state == State.FIRED
    assert len(d._fires) == 1


def test_re_armed_then_fires_again():
    d = _det()
    with patch("httpx.post") as p:
        p.return_value.status_code = 200
        for _ in range(3):
            d.evaluate(BROKEN)
        for _ in range(3):
            d.evaluate(HEALTHY)
        assert d.state == State.WATCHING
        for _ in range(3):
            d.evaluate(BROKEN)
    assert d.state == State.FIRED
    assert len(d._fires) == 2
