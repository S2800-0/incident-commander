"""Policy engine client — real OPA + Rego over HTTP, not a Python re-implementation.

The orchestrator asks THIS module "is this action allowed?" before executing any
state-changing operation (rollback, canary, bounded intervention). This module
POSTs the input to OPA's Data API and returns the structured decision.

Design rules (do not violate):
  * Decision logic (thresholds, envelope validation, precondition checks) lives
    in `policy/incident/action.rego`. NEVER duplicate it here.
  * A broken/unreachable OPA fails closed — the orchestrator gets `deny` with
    reason `policy_engine_unavailable`. Fail-open would defeat the whole point.
  * Every decision is serialised into a dict shape suitable for logging into
    the evidence chain, so a compliance reviewer can point to the exact policy
    result that permitted (or denied) any given action.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Optional

# HTTP client kept intentionally on the stdlib — one less dep, and this module's
# only external call is a single POST to a well-known endpoint.

# 127.0.0.1, not localhost: on Windows, `localhost` resolves to ::1 first and a
# refused IPv6 connect costs ~2s before IPv4 is tried — every policy decision paid it.
OPA_URL = os.environ.get("IC_OPA_URL", "http://127.0.0.1:8181")
POLICY_PATH = "/v1/data/incident/action/decision"
TIMEOUT_S = float(os.environ.get("IC_OPA_TIMEOUT_S", "2.0"))


@dataclass
class PolicyDecision:
    allow: bool
    action: str
    reasons: list[str] = field(default_factory=list)  # empty when allow=True
    thresholds: dict = field(default_factory=dict)     # what OPA used to decide
    input_doc: dict = field(default_factory=dict)      # what we sent
    engine_available: bool = True                       # False = fail-closed deny

    def denied(self) -> bool:
        return not self.allow

    def to_event(self) -> dict:
        """Shape suitable for the evidence chain / event stream."""
        return {
            "type": "policy_decision",
            "action": self.action,
            "allow": self.allow,
            "reasons": list(self.reasons),
            "thresholds": dict(self.thresholds),
            "engine_available": self.engine_available,
        }


class PolicyEngineError(RuntimeError):
    """OPA is unreachable or returned an unparseable response."""


def _http_post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def evaluate(input_doc: dict) -> PolicyDecision:
    """Ask OPA. Raises PolicyEngineError on network/OPA failure."""
    try:
        body = _http_post_json(f"{OPA_URL}{POLICY_PATH}",
                                {"input": input_doc}, TIMEOUT_S)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise PolicyEngineError(f"OPA request failed: {e}") from e
    except json.JSONDecodeError as e:
        raise PolicyEngineError(f"OPA returned non-JSON: {e}") from e

    result = body.get("result") or {}
    if not isinstance(result, dict) or "allow" not in result:
        raise PolicyEngineError(f"unexpected OPA response shape: {body!r}")

    return PolicyDecision(
        allow=bool(result["allow"]),
        action=str(result.get("action", input_doc.get("action", "unknown"))),
        reasons=list(result.get("reasons", []) or []),
        thresholds=dict(result.get("thresholds", {}) or {}),
        input_doc=dict(input_doc),
        engine_available=True,
    )


def evaluate_safely(input_doc: dict) -> PolicyDecision:
    """Ask OPA. On any failure, return a fail-closed deny with a clear reason —
    the orchestrator can log it and skip the action. This is the entry point
    the orchestrator uses; production infrastructure must NEVER autonomously act
    while the policy engine is down."""
    try:
        return evaluate(input_doc)
    except PolicyEngineError as e:
        return PolicyDecision(
            allow=False,
            action=str(input_doc.get("action", "unknown")),
            reasons=["policy_engine_unavailable", str(e)[:200]],
            input_doc=dict(input_doc),
            engine_available=False,
        )


# --- Input builders ---------------------------------------------------------
# The orchestrator constructs the input doc from concrete state at the gate
# point; these helpers keep the shape consistent with the Rego contract.

def build_input_for_rollback(
    incident_id: str,
    posterior: float,
    evidence_refs: list[str],
    deploy_recent: bool = True,
    upstream_outage: bool = False,
) -> dict:
    return {
        "action": "rollback",
        "incident_id": incident_id,
        "posterior": float(posterior),
        "evidence_refs": list(evidence_refs),
        "deploy_recent": bool(deploy_recent),
        "upstream_outage": bool(upstream_outage),
        "observation_stagnated": False,   # not applicable to rollback
        "safety_envelope": {},             # rollback carries no envelope
    }


def build_input_for_intervention(
    incident_id: str,
    posterior: float,
    evidence_refs: list[str],
    safety_envelope: dict,
    observation_stagnated: bool = True,
    action: str = "intervention",
) -> dict:
    return {
        "action": action,   # "intervention" (tighter) or "canary" (wider)
        "incident_id": incident_id,
        "posterior": float(posterior),
        "evidence_refs": list(evidence_refs),
        "deploy_recent": True,
        "upstream_outage": False,
        "observation_stagnated": bool(observation_stagnated),
        "safety_envelope": dict(safety_envelope or {}),
    }


# --- Optional CLI probe -----------------------------------------------------
# `python -m ic.policy` = health check + one sample decision. Useful for
# verifying OPA is reachable before starting the demo.

def _cli_probe() -> int:
    sample = build_input_for_intervention(
        incident_id="INC-DEMO",
        posterior=0.6,
        evidence_refs=["log://demo/probe1"],
        safety_envelope={"max_traffic_pct": 5, "max_duration_s": 60, "auto_revert": True},
        observation_stagnated=True,
    )
    decision = evaluate_safely(sample)
    print(json.dumps(asdict(decision), indent=2, default=str))
    return 0 if decision.engine_available else 1


if __name__ == "__main__":
    raise SystemExit(_cli_probe())
