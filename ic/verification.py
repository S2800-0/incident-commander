"""Post-intervention verification loop.

The mechanism until now: pick a next-best action (VoI adjudicator), execute it
(policy-gated), update the beliefs, seal the evidence chain. That answers
*what happened* but not *did the fix actually work*.

This module adds the closed-loop half — after an intervention runs, we
verify that the customer-facing (or discriminator) signal actually recovered.
Two paths out:

  * **Verified recovery** — the signal that triggered the incident is back
    within the recovery band; the verdict is sealed with
    `verified_recovery=True` and provenance stays `interventional`.

  * **Recovery not observed** — the intervention did not fix the problem. The
    safety-envelope's `auto_revert` promise fires (structurally — the canary
    traffic is returned to baseline), the verdict is marked
    `verified_recovery=False`, and the audit log records BOTH the intervention
    outcome AND the revert. Provenance falls back to `observational` because
    the intervention did not conclude the incident.

Why this belongs in its own module: the check is a real decision (band
comparison + a source-of-recovery), not a formatting concern. Threshold
tuning, band definitions, and the auto-revert protocol are all going to
evolve; keeping them here means the orchestrator stays a scheduler, not a
verification engine.

Real-production note: the recovery signal in a live deployment is a
customer-facing SLI (checkout success rate, p95 latency, payment success).
For the demo, the signal comes from the bundle's `verification_result` block
(spec below); when absent, we synthesise a result from ground truth so
existing bundles keep working without a schema flag day.

Bundle schema addition (optional, backward-compatible):

    "evidence": {
      ...,
      "verification_result": {
        "signal": "http.server.response.5xx.rate",
        "baseline_value": 2.1,
        "post_intervention_value": 3.5,
        "recovery_band": {"max": 5.0},      # signal ≤ max ⇒ recovered
        "measured_at": "2025-11-11T00:12:00Z",
        "source_uri": "otlp://checkout-service/metric/http.server.response.5xx.rate",
        "_note_for_authors": "Author only — never surfaced to agents."
      }
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .bundle import Bundle
from .models import EvidenceItem, Hypothesis


# --- Result type ------------------------------------------------------------

@dataclass
class VerificationOutcome:
    recovered: bool
    signal: str
    baseline_value: Optional[float]
    post_value: Optional[float]
    recovery_band: dict
    source_uri: str
    reasoning: str
    evidence_item: EvidenceItem
    synthesised: bool                    # True when we had to infer from ground truth
    engine_available: bool = True        # False when verification was skipped


# --- Public entry point -----------------------------------------------------

def run_verification(bundle: Bundle,
                     intervention_id: str,
                     session: list[EvidenceItem],
                     winner: Optional[Hypothesis] = None) -> VerificationOutcome:
    """Check whether the intervention actually resolved the incident.

    * Prefers an explicit `verification_result` block on the bundle (real
      production shape: read a customer signal and compare to a threshold).
    * Falls back to synthesising from ground truth + winner alignment so
      existing bundles that predate this schema addition still work.

    Never raises — a verification we cannot compute returns `recovered=False`
    with the reason logged, and the orchestrator treats that as revert-worthy.
    """
    block = _explicit_block(bundle)
    if block is not None:
        return _from_explicit_block(bundle, intervention_id, block)

    # Synthesised path — no explicit block, infer from ground truth.
    return _synthesise_from_ground_truth(bundle, intervention_id, winner)


# --- Implementation ---------------------------------------------------------

def _explicit_block(bundle: Bundle) -> Optional[dict]:
    """Return the raw verification_result block, or None if absent."""
    ev = bundle.raw.get("evidence", {})
    block = ev.get("verification_result")
    if not isinstance(block, dict):
        return None
    return block


def _from_explicit_block(bundle: Bundle, intervention_id: str, block: dict) -> VerificationOutcome:
    baseline = _num(block.get("baseline_value"))
    post = _num(block.get("post_intervention_value"))
    band = block.get("recovery_band") or {}
    signal = str(block.get("signal") or "unknown_signal")
    source_uri = str(block.get("source_uri") or f"verification://{bundle.incident_id}/{signal}")

    recovered = _within_band(post, band) if post is not None else False
    reasoning = _explain_band(post, band, recovered)

    item = EvidenceItem(
        source_type="verification",
        source_uri=source_uri,
        retrieved_at=str(block.get("measured_at") or ""),
        measures=[signal],
        payload={
            "kind":                     "post_intervention_recovery_check",
            "intervention_id":          intervention_id,
            "signal":                   signal,
            "baseline_value":           baseline,
            "post_intervention_value":  post,
            "recovery_band":            band,
            "recovered":                recovered,
            "reasoning":                reasoning,
            "source":                   "explicit_bundle_block",
        },
    )
    return VerificationOutcome(
        recovered=recovered,
        signal=signal,
        baseline_value=baseline,
        post_value=post,
        recovery_band=band,
        source_uri=source_uri,
        reasoning=reasoning,
        evidence_item=item,
        synthesised=False,
    )


def _synthesise_from_ground_truth(bundle: Bundle, intervention_id: str,
                                  winner: Optional[Hypothesis]) -> VerificationOutcome:
    """Fallback when a bundle has no explicit verification_result. If the
    winning hypothesis after intervention matches ground truth AND rollback is
    the correct call, we treat the intervention as having identified the fix
    and mark recovered=True. This is honest: verification here reflects
    "the intervention correctly identified the cause," not "we watched a live
    customer signal." The event stream and the sealed chain always say which
    path produced the outcome."""
    gt_root = str(bundle.ground_truth.get("root_cause_id") or "")
    rb_correct = bool(bundle.ground_truth.get("rollback_correct", False))
    winner_id = winner.id if winner else ""

    match = bool(winner_id) and (winner_id == gt_root)
    recovered = match and rb_correct

    reasoning = (
        f"winner={winner_id!r} matches ground truth root_cause_id={gt_root!r}; "
        f"rollback_correct={rb_correct} — recovery {'confirmed' if recovered else 'not confirmed'}"
    )

    signal = "ground_truth_alignment"
    source_uri = f"verification://{bundle.incident_id}/synthesised"
    item = EvidenceItem(
        source_type="verification",
        source_uri=source_uri,
        retrieved_at="",
        measures=[signal],
        payload={
            "kind":                "post_intervention_recovery_check",
            "intervention_id":     intervention_id,
            "winner":              winner_id,
            "ground_truth":        gt_root,
            "rollback_correct":    rb_correct,
            "recovered":           recovered,
            "reasoning":           reasoning,
            "source":              "synthesised_from_ground_truth",
        },
    )
    return VerificationOutcome(
        recovered=recovered,
        signal=signal,
        baseline_value=None,
        post_value=None,
        recovery_band={},
        source_uri=source_uri,
        reasoning=reasoning,
        evidence_item=item,
        synthesised=True,
    )


# --- Utility ---------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _within_band(value: float, band: dict) -> bool:
    lo = _num(band.get("min"))
    hi = _num(band.get("max"))
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def _explain_band(value: Optional[float], band: dict, recovered: bool) -> str:
    if value is None:
        return "no post_intervention_value in bundle — treated as not recovered"
    lo = _num(band.get("min"))
    hi = _num(band.get("max"))
    parts = []
    if lo is not None:
        parts.append(f"≥ {lo}")
    if hi is not None:
        parts.append(f"≤ {hi}")
    band_str = " and ".join(parts) if parts else "no band"
    verdict = "within" if recovered else "outside"
    return f"post_intervention_value={value} is {verdict} recovery band ({band_str})"
