"""Dynamic hypothesis generation — LLM-driven triage.

Complements the deterministic `ic.agents.triage.seed_hypotheses(bundle)` used
by the graded harness. When `IC_DYNAMIC_HYPOTHESES=1` and a live LLM is
available, this module generates hypotheses at runtime from the incident
context, producing structures that carry the six fields the plan requires:

    claim
    supporting_evidence      # source_uris from the prior evidence tier
    contradicting_evidence   # same — evidence that argues AGAINST the claim
    predicted_observations   # per-observable predictions used by the reasoner
    discriminating_signals   # what observation would tell hypotheses apart
    confidence               # LLM's own confidence, distinct from `posterior`

Design rules:
  * The dynamic mode is OPT-IN — env flag `IC_DYNAMIC_HYPOTHESES=1` OR the
    orchestrator argument `dynamic_hypotheses=True`. Never on by default.
  * When enabled but the LLM is not available (no key / IC_USE_LLM=0), we
    transparently fall back to `seed_hypotheses` and emit a
    `dynamic_generation_fallback` event so the reason is auditable.
  * The graded harness explicitly opts out (see ic.harness), so ablation
    numbers stay reproducible regardless of any env state.
  * Ground truth is NEVER included in the prompt. Neither are seed
    hypotheses — the whole point is generation from raw context.
  * Downstream code (agents, adjudicator, VoI, reasoner) does not need to
    change: a Hypothesis is a Hypothesis, and the enrichment fields are
    additive.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from .agents import triage
from .bundle import Bundle
from .llm import json_call, llm_enabled
from .models import Hypothesis, PredictedObservation

# ============================================================================
# LLM-response schema
# ============================================================================

class DynamicHypothesis(BaseModel):
    """Strict schema the LLM must satisfy — validated by ic.llm.json_call."""
    id: str = Field(description="Short id like 'H1', 'H2'")
    claim: str = Field(description="One-sentence root-cause hypothesis")
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description="source_uris from provided prior evidence that support this hypothesis",
    )
    contradicting_evidence: list[str] = Field(
        default_factory=list,
        description="source_uris that argue against this hypothesis (be honest)",
    )
    predicted_observations: list[dict] = Field(
        description="Per-observable prediction: {observable, prediction, note}",
    )
    discriminating_signals: list[str] = Field(
        default_factory=list,
        description="Observable names that would distinguish this hypothesis from others",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Your prior confidence in [0,1]")
    natural_owner: str = Field(
        default="triage",
        description="Which agent (change_agent | telemetry_agent | history_agent | triage) "
                    "should own tracking this hypothesis",
    )


class DynamicHypothesesResponse(BaseModel):
    hypotheses: list[DynamicHypothesis] = Field(
        min_length=2, max_length=4,
        description="Two to four competing hypotheses — must actually differ in root cause",
    )
    reasoning_summary: str = Field(
        default="",
        description="One-paragraph summary of why these hypotheses were chosen",
    )


# ============================================================================
# Public entry point
# ============================================================================

def dynamic_enabled_from_env() -> bool:
    return os.environ.get("IC_DYNAMIC_HYPOTHESES", "").lower() in ("1", "true", "yes", "on")


def dynamic_seed_hypotheses(
    bundle: Bundle,
    emit=None,
) -> tuple[list[Hypothesis], dict]:
    """Generate hypotheses dynamically from bundle context, or fall back to
    seeded with a clear reason. Returns (hypotheses, meta) — meta describes
    which path fired so the orchestrator can emit the right events."""
    emit = emit or (lambda _e: None)

    if not llm_enabled():
        # Fallback: LLM not configured. Return seeded, name the reason.
        meta = {"mode": "fallback", "reason": "llm_disabled_or_no_key"}
        emit({"type": "dynamic_generation_fallback", **meta,
              "note": "IC_USE_LLM=0 or no provider key — using seeded hypotheses"})
        return triage.seed_hypotheses(bundle), meta

    system_prompt = _system_prompt()
    user_prompt = _user_prompt(bundle)

    emit({"type": "dynamic_generation_started",
          "context_size_chars": len(user_prompt),
          "provider_chain_active": True})

    try:
        resp = json_call(system_prompt, user_prompt, DynamicHypothesesResponse,
                         max_tokens=1800, temperature=0.3)
    except (RuntimeError, ValidationError) as e:
        meta = {"mode": "fallback", "reason": "llm_error",
                "error": str(e).splitlines()[0][:200]}
        emit({"type": "dynamic_generation_fallback", **meta,
              "note": "LLM call failed — using seeded hypotheses so the demo stays honest"})
        return triage.seed_hypotheses(bundle), meta

    # Validate that the LLM referenced only real evidence refs.
    resolvable = {e.ref() for e in bundle.prior_evidence()}
    hyps: list[Hypothesis] = []
    for dh in resp.hypotheses:
        h = _to_hypothesis(dh, resolvable)
        hyps.append(h)
        emit({"type": "hypothesis_generated",
              "id": h.id,
              "claim": h.claim,
              "confidence": h.confidence,
              "supporting_evidence": h.supporting_evidence,
              "contradicting_evidence": h.contradicting_evidence,
              "discriminating_signals": h.discriminating_signals,
              "natural_owner": h.agent_id})

    emit({"type": "dynamic_generation_completed",
          "count": len(hyps),
          "reasoning_summary": resp.reasoning_summary})

    return hyps, {"mode": "dynamic", "count": len(hyps),
                  "reasoning_summary": resp.reasoning_summary}


# ============================================================================
# Prompt construction — context WITHOUT ground truth
# ============================================================================

_ALLOWED_OWNERS = {"change_agent", "telemetry_agent", "history_agent", "triage"}


def _system_prompt() -> str:
    return (
        "You are the triage layer of a production-incident differential diagnosis "
        "system. Given the alert, topology, prior evidence, and observables schema, "
        "produce 2–4 COMPETING root-cause hypotheses that a human on-call would "
        "actually consider first.\n\n"
        "Rules:\n"
        "  * Each hypothesis must have a distinct root cause — not variations of "
        "    the same cause.\n"
        "  * Each hypothesis must cite at least one source_uri from the prior "
        "    evidence as supporting_evidence. Do not invent source_uris.\n"
        "  * If any prior evidence argues AGAINST a hypothesis, include it under "
        "    contradicting_evidence. Honesty about weak evidence is a feature.\n"
        "  * predicted_observations must reference observable names from the "
        "    schema. Format: {observable, prediction, note}.\n"
        "  * discriminating_signals name observables that would separate this "
        "    hypothesis from the others.\n"
        "  * natural_owner MUST be one of: change_agent, telemetry_agent, "
        "    history_agent, triage.\n"
        "  * confidence is your prior in [0, 1] — not certainty; the mechanism "
        "    updates posteriors via evidence, not your say-so."
    )


def _user_prompt(bundle: Bundle) -> str:
    lines: list[str] = []
    a = bundle.alert
    lines.append(f"# Incident {bundle.incident_id}")
    lines.append("")
    lines.append("## Alert")
    for k in ("service", "environment", "severity", "signal", "condition", "fired_at"):
        if k in a:
            lines.append(f"  {k}: {a[k]}")
    lines.append("")
    if bundle.topology:
        lines.append("## Topology")
        for n in bundle.topology.get("nodes", []):
            lines.append(f"  node: {n}")
        for e in bundle.topology.get("edges", []):
            lines.append(f"  edge: {e.get('from')} -> {e.get('to')}")
        lines.append("")
    if bundle.observables:
        lines.append("## Observables schema (what these signals mean)")
        for o in bundle.observables:
            lines.append(f"  {o['name']:28s}  unit={o.get('unit','')}  — {o.get('desc','')}")
        lines.append("")
    lines.append("## Prior evidence (available to all agents at t=0)")
    for it in bundle.prior_evidence():
        lines.append(f"  source_uri: {it.source_uri}")
        lines.append(f"    source_type: {it.source_type}")
        lines.append(f"    measures:    {list(it.measures)}")
        # Include a compact payload preview so the LLM can reason about it.
        preview = _compact(it.payload)
        lines.append(f"    payload:     {preview}")
        lines.append("")
    lines.append("Produce two to four competing hypotheses per the rules.")
    return "\n".join(lines)


def _compact(value: Any, max_chars: int = 320) -> str:
    """Truncated single-line rendering — prompt-length-friendly but faithful."""
    import json as _json
    try:
        s = _json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= max_chars else s[: max_chars - 3] + "..."


# ============================================================================
# LLM output → Hypothesis
# ============================================================================

def _to_hypothesis(dh: DynamicHypothesis, resolvable_refs: set[str]) -> Hypothesis:
    """Convert to the internal type. Strip supporting/contradicting refs that
    do not resolve to any real prior evidence (anti-hallucination)."""
    supp = [r for r in dh.supporting_evidence if r in resolvable_refs]
    contra = [r for r in dh.contradicting_evidence if r in resolvable_refs]
    owner = dh.natural_owner if dh.natural_owner in _ALLOWED_OWNERS else "triage"
    predictions = [PredictedObservation(**_clean_prediction(po))
                   for po in dh.predicted_observations
                   if isinstance(po, dict) and po.get("observable")]
    return Hypothesis(
        id=_sanitize_id(dh.id),
        claim=dh.claim.strip(),
        agent_id=owner,
        predicted_observations=predictions,
        supporting_evidence=supp,
        contradicting_evidence=contra,
        discriminating_signals=list(dict.fromkeys(dh.discriminating_signals)),
        confidence=float(dh.confidence),
        generated_by="dynamic",
        # evidence_refs seeds from supporting_evidence so downstream anti-
        # hallucination logic still sees a resolvable citation set.
        evidence_refs=list(supp),
    )


def _clean_prediction(po: dict) -> dict:
    return {
        "observable": str(po.get("observable", ""))[:80],
        "prediction": str(po.get("prediction", ""))[:120],
        "note":       str(po.get("note", ""))[:200],
    }


def _sanitize_id(raw: str) -> str:
    m = re.match(r"[HhTt]?(\d{1,3})", raw.strip())
    return f"H{m.group(1)}" if m else f"H{abs(hash(raw)) % 9000 + 1}"
