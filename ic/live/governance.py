"""Policy gate for live mode.

Every state-changing action — interventional probes, remediations, reverts —
passes through `authorize`, which asks the real OPA server (ic/policy.py) and
turns the answer into an evidence leaf. There is no other path to an action,
so there is no ALLOW or DENY that is not sealed into the incident's chain.
OPA unreachable ⇒ fail-closed DENY, recorded the same way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..models import EvidenceItem
from ..policy import OPA_URL, POLICY_PATH, PolicyDecision, evaluate_safely
from .probes import iso

import time


@dataclass
class Authorization:
    decision: PolicyDecision
    evidence: EvidenceItem

    @property
    def allow(self) -> bool:
        return self.decision.allow


def authorize(incident_id: str, seq: int, action: str, *, evidence_refs: list[str],
              reversible: bool, blast_radius: str, posterior: float = 0.0,
              deploy_recent: bool = False, upstream_outage: bool = False,
              confirmation: str = "none", verification_planned: bool = False,
              safety_envelope: Optional[dict] = None, reverts_agent_action: bool = False,
              extra: Optional[dict] = None) -> Authorization:
    input_doc = {
        "mode": "live",
        "action": action,
        "incident_id": incident_id,
        "posterior": round(float(posterior), 4),
        "evidence_refs": sorted(set(evidence_refs)),
        "deploy_recent": bool(deploy_recent),
        "upstream_outage": bool(upstream_outage),
        "observation_stagnated": False,
        "reversible": bool(reversible),
        "blast_radius": blast_radius,
        "confirmation": confirmation,
        "verification_planned": bool(verification_planned),
        "safety_envelope": dict(safety_envelope or {}),
        "reverts_agent_action": bool(reverts_agent_action),
    }
    decision = evaluate_safely(input_doc)
    item = EvidenceItem(
        source_type="policy_decision",
        source_uri=f"opa://incident/action/decision/{incident_id}/{seq:02d}-{action}",
        retrieved_at=iso(time.time()),
        measures=[],
        payload={
            "engine": f"OPA {OPA_URL}{POLICY_PATH}",
            "engine_available": decision.engine_available,
            "input": input_doc,
            "result": {"allow": decision.allow, "reasons": sorted(decision.reasons),
                       "thresholds": decision.thresholds},
            **({"context": extra} if extra else {}),
        },
    )
    return Authorization(decision, item)
