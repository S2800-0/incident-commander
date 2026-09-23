"""Live mode — Incident Commander against a running service.

    target service ──OTLP──▶ TelemetryStore ──▶ Detector ──▶ LiveController
                                   ▲                              │
                                   └──── probes / verification ◀──┤
    target /ops API ◀── policy-gated actions (OPA) ◀──────────────┘

Replay mode (ic/orchestrator.py + ic/harness.py) is untouched by this package;
its 12-bundle evaluation stays deterministic and reproducible.
"""
