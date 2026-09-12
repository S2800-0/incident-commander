"""Pydantic data model mirroring schema/incident_bundle.schema.md.

All hashing goes through `canonical_json` so hashes are byte-for-byte reproducible
across processes and machines. This is load-bearing for the evidence chain.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding used everywhere a hash is computed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class EvidenceItem(BaseModel):
    source_type: str
    source_uri: str
    retrieved_at: Optional[str] = None
    measures: list[str] = Field(default_factory=list)
    probe_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)

    def content_hash(self) -> str:
        """sha256(canonical_json(payload) || source_uri || retrieved_at)."""
        basis = canonical_json(self.payload) + self.source_uri + str(self.retrieved_at)
        return sha256_hex(basis)

    def ref(self) -> str:
        """Stable short reference id used by agents to cite evidence."""
        return self.source_uri


class PredictedObservation(BaseModel):
    observable: str
    prediction: str
    note: str = ""


class Hypothesis(BaseModel):
    id: str
    claim: str
    agent_id: str
    posterior: float = 0.0
    predicted_observations: list[PredictedObservation] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    eliminated: bool = False
    eliminated_reason: str = ""
    # Optional dynamic-triage enrichment (ic/dynamic_triage.py). Populated
    # only when hypotheses are LLM-generated; seeded hypotheses leave these
    # at their defaults so the graded harness stays byte-identical.
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    discriminating_signals: list[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    generated_by: Literal["seeded", "dynamic"] = "seeded"


class Probe(BaseModel):
    probe_id: str
    connector: str
    measures: list[str] = Field(default_factory=list)
    cost_ms: int = 0
    load_class: str = "read_light"
    description: str = ""


class ActionKind(str, Enum):
    OBSERVATION = "observation"
    INTERVENTION = "intervention"


class CandidateAction(BaseModel):
    """One candidate next action in the joint observation/intervention space.
    Every Probe maps to kind=OBSERVATION; the intervention placeholder is the one
    kind=INTERVENTION entry (executable=False in the prototype)."""
    action_id: str
    kind: ActionKind
    description: str                       # human-readable, shown in UI
    measures: list[str] = Field(default_factory=list)

    # VoI components — all computed by the VoI Engine (ic/voi.py)
    eig: float = 0.0                       # expected information gain, nats
    cost: float = 0.0                      # unitless, normalized to probe cost_ms
    risk: float = 0.0                      # 0.0 for observation; nonzero for intervention
    voi_score: float = 0.0                 # eig - LAMBDA*cost - MU*risk

    # Intervention-only metadata (None for observation actions)
    executable: bool = True                # False for the placeholder — CRITICAL
    unavailable_reason: Optional[str] = None   # e.g. "not_available_in_prototype"
    safety_envelope: Optional[dict] = None     # bounded %, timeout, revert condition


class Verdict(BaseModel):
    incident_id: str
    root_cause_id: str
    summary: str
    posterior: float
    rollback_recommended: bool
    hypotheses: list[Hypothesis]
    probes_run: list[str]
    evidence_refs: list[str]
    merkle_root: Optional[str] = None
    signature: Optional[str] = None
    # VoI overlay: always "observational" in the prototype — the intervention
    # primitive is specified but never executed (see ic/voi.py).
    provenance: Literal["observational", "interventional"] = "observational"
    # Post-intervention verification (verification.py). None when the loop
    # never ran (no intervention or policy_enabled=False); True when the
    # customer-facing signal returned to the recovery band; False when the
    # intervention did not resolve the incident and auto-revert fired.
    verified_recovery: Optional[bool] = None
    # Customer-impact score (cis.py). Operational urgency, ORTHOGONAL to
    # diagnostic scoring — never fed into hypothesis posteriors or VoI probe
    # selection. None when the bundle carries no customer_impact block.
    customer_impact: Optional[dict] = None
