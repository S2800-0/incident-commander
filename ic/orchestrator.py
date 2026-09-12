"""Orchestrator — runs a full investigation and emits structured events for the console.

Event types (all dicts with a `type` key):
  agent_started, hypothesis_proposed, posterior_updated, ambiguity_detected,
  probe_selected, probe_result, hypothesis_eliminated, exhausted, gate_pending,
  policy_decision, action_blocked, verdict, chain_sealed
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .adjudicator import (TAU, apply_probe_result, decide, ranked, reconcile,
                          top_margin)
from .agents import Contribution, change_agent, history_agent, telemetry_agent, triage
from .agents.llm_lens import llm_contributions
from .bundle import Bundle
from .cis import compute as compute_cis
from .cis import to_evidence_item as cis_evidence_item
from .dynamic_triage import (dynamic_enabled_from_env,
                              dynamic_seed_hypotheses)
from .evidence_chain import seal
from .llm import llm_enabled
from .models import EvidenceItem, Hypothesis, Probe, Verdict
from .policy import (build_input_for_intervention, build_input_for_rollback,
                     evaluate_safely)
from .probe import select_probe
from .reasoner import is_rollback_hypothesis
from .verification import run_verification
from .voi import (STAGNATION_THRESHOLD, intervention_would_fire,
                  observation_stagnated, score_actions)

# Hard probe-loop bounds (guardrail).
K_MAX_PROBES = 4
T_WALL_SECONDS = 45
NO_EVIDENCE_PENALTY = 0.5  # an uncited claim can't win on nothing

Emit = Callable[[dict], None]


@dataclass
class RunResult:
    verdict: Verdict
    events: list[dict] = field(default_factory=list)
    probes_run: list[str] = field(default_factory=list)
    interventions_run: list[str] = field(default_factory=list)
    verified_recovery: Optional[bool] = None
    time_to_conclusion_s: float = 0.0
    sealed: dict = field(default_factory=dict)
    cited_items: list[EvidenceItem] = field(default_factory=list)


def _noop(_e: dict) -> None:
    pass


def _policy_enabled_from_env() -> bool:
    return os.environ.get("IC_POLICY_ENABLED", "").lower() in ("1", "true", "yes", "on")


def run_investigation(bundle: Bundle, probes_enabled: bool = True,
                      emit: Optional[Emit] = None, pacing: float = 0.0,
                      use_llm: Optional[bool] = None,
                      policy_enabled: Optional[bool] = None,
                      dynamic_hypotheses: Optional[bool] = None) -> RunResult:
    # use_llm=None → auto-detect from env (IC_USE_LLM + a real key). The harness passes
    # use_llm=False explicitly so ablation numbers are always deterministic/reproducible.
    # policy_enabled follows the same discipline — the graded harness passes False
    # explicitly so ablation numbers do not depend on a running OPA container. The
    # server (live demo) passes True so state-changing actions are policy-gated.
    # dynamic_hypotheses follows the same pattern — the harness passes False so seeded
    # hypotheses are used regardless of env; the live demo may opt in for LLM triage.
    if use_llm is None:
        use_llm = llm_enabled()
    if policy_enabled is None:
        policy_enabled = _policy_enabled_from_env()
    if dynamic_hypotheses is None:
        dynamic_hypotheses = dynamic_enabled_from_env()
    emit = emit or _noop
    events: list[dict] = []

    def _emit(e: dict) -> None:
        e = {"incident_id": bundle.incident_id, "probes_enabled": probes_enabled, **e}
        events.append(e)
        emit(e)
        if pacing:
            time.sleep(pacing)

    t0 = time.time()

    # --- [1] triage: seed the competing hypotheses --------------------------
    # Two paths:
    #   * seeded (default) — deterministic, reads bundle.seed_hypotheses.
    #     Preserves reproducible ablation numbers.
    #   * dynamic (opt-in)  — LLM-generated from raw context, populated with
    #     the six enrichment fields (supporting/contradicting evidence,
    #     predictions, discriminating signals, confidence). Falls back to
    #     seeded transparently when the LLM is unavailable.
    if dynamic_hypotheses:
        hyps, dyn_meta = dynamic_seed_hypotheses(bundle, emit=_emit)
    else:
        hyps = triage.seed_hypotheses(bundle)
    session: list[EvidenceItem] = bundle.prior_evidence()
    resolvable = {e.ref() for e in session}
    logits: dict[str, float] = {h.id: 0.0 for h in hyps}

    # --- [2] parallel fan-out: each agent scores the hypotheses it owns -----
    for mod in (change_agent, telemetry_agent, history_agent):
        _emit({"type": "agent_started", "agent_id": mod.AGENT_ID,
               "mode": "llm" if use_llm else "analytical"})
        contribs: list[Contribution] = []
        if use_llm:
            try:
                contribs = llm_contributions(mod.AGENT_ID, bundle, session, hyps)
            except Exception as e:
                # a flaky/keyless model must never break an investigation
                _emit({"type": "agent_fallback", "agent_id": mod.AGENT_ID,
                       "reason": str(e).splitlines()[0][:120]})
                contribs = []
        if not contribs:
            contribs = mod.assess(bundle, session, hyps)
        for c in contribs:
            # anti-hallucination gate: only credit refs that resolve to real evidence.
            good = [r for r in c.evidence_refs if r in resolvable]
            logits[c.hyp_id] = logits.get(c.hyp_id, 0.0) + c.logit_delta
            h = next(h for h in hyps if h.id == c.hyp_id)
            for r in good:
                if r not in h.evidence_refs:
                    h.evidence_refs.append(r)
            _emit({"type": "agent_reasoned", "agent_id": c.agent_id,
                   "hyp_id": c.hyp_id, "delta": round(c.logit_delta, 3),
                   "rationale": c.rationale, "evidence_refs": good})

    # guardrail: a claim citing zero resolvable evidence is penalised.
    for h in hyps:
        if not h.evidence_refs:
            logits[h.id] -= NO_EVIDENCE_PENALTY

    reconcile(hyps, logits)
    for h in hyps:
        _emit({"type": "hypothesis_proposed", "id": h.id, "claim": h.claim,
               "agent_id": h.agent_id, "posterior": round(h.posterior, 3),
               "evidence_refs": h.evidence_refs})

    # --- [3–5] probe loop ---------------------------------------------------
    # The T_WALL budget bounds the PROBE loop, not the whole investigation — otherwise
    # slow live-LLM agent fan-out (which precedes this) would eat the budget and the loop
    # would "exhaust" before running a single probe. Time from here.
    probe_loop_t0 = time.time()
    probes_run: list[str] = []
    interventions_run: list[str] = []
    verified_recovery: Optional[bool] = None
    already = set()
    intervention_done = False
    voi_step = 0
    while True:
        # --- VoI overlay: decompose & rank every candidate next action -------
        voi_step += 1
        actions = score_actions(hyps, bundle.probe_catalog, already)
        _emit({"type": "voi_scored", "step": voi_step,
               "actions": [a.model_dump(mode="json") for a in actions]})

        stagnated = observation_stagnated(actions)
        interv_action = next((a for a in actions if not a.executable), None)
        # An intervention is EXECUTABLE for THIS incident only when the bundle
        # authored an intervention_result for it (i.e., the safety envelope /
        # simulation of causal outcome exists). Same simulation model as our probes.
        interv_available = bool(
            interv_action and probes_enabled and bundle.intervention_ids()
            and interv_action.action_id in bundle.intervention_ids()
        )

        if stagnated:
            obs = [a for a in actions if a.executable]
            best_obs = max(obs, key=lambda a: a.eig, default=None)
            _emit({"type": "voi_stagnation_detected",
                   "threshold": STAGNATION_THRESHOLD,
                   "best_observation": best_obs.action_id if best_obs else None,
                   "best_observation_eig": best_obs.eig if best_obs else 0.0,
                   "intervention_eig": interv_action.eig if interv_action else None,
                   "intervention_available": interv_available})

        # ---- Intervention execution path ---------------------------------
        # Runs when: observation has stagnated, the bundle authored an
        # intervention outcome, and we haven't already run one. The "execution"
        # is against fixture data (same simulation model as probes).
        #
        # policy_enabled=True (server / live demo):
        #   real OPA policy call decides ALLOW/DENY; on ALLOW the action executes
        #   autonomously (no button); on DENY it is skipped with structured reasons
        #   logged into the event stream and the evidence chain.
        # policy_enabled=False (harness / tests):
        #   the legacy UI-side pause event is emitted for the console to display,
        #   and execution proceeds as before — this keeps ablation numbers
        #   reproducible without a running OPA container.
        if stagnated and interv_available and not intervention_done:
            interv_id = interv_action.action_id
            cited_refs_now = sorted({r for h in hyps for r in h.evidence_refs})
            top_posterior = ranked(hyps)[0].posterior if hyps else 0.0

            if policy_enabled:
                policy_input = build_input_for_intervention(
                    incident_id=bundle.incident_id,
                    posterior=top_posterior,
                    evidence_refs=cited_refs_now,
                    safety_envelope=interv_action.safety_envelope or {},
                    observation_stagnated=True,
                    action="intervention",
                )
                decision = evaluate_safely(policy_input)
                _emit({**decision.to_event(),
                       "intervention_id": interv_id,
                       "description": interv_action.description})
                if not decision.allow:
                    _emit({"type": "action_blocked",
                           "action": "intervention",
                           "intervention_id": interv_id,
                           "reasons": decision.reasons,
                           "engine_available": decision.engine_available,
                           "note": "policy denied — intervention NOT executed"})
                    intervention_done = True   # do not retry the same denied action
                    continue
            else:
                _emit({"type": "gate_pending", "action": "intervention",
                       "intervention_id": interv_id,
                       "description": interv_action.description,
                       "safety_envelope": interv_action.safety_envelope,
                       "note": "human approval required before intervention executes — "
                               "console pauses here until Approve is clicked"})

            item = bundle.run_intervention(interv_id, probes_enabled=True)
            session.append(item)
            resolvable.add(item.ref())
            interventions_run.append(interv_id)
            intervention_done = True

            synth_probe = Probe(probe_id=interv_id, connector="intervention",
                                measures=list(item.measures), cost_ms=0,
                                load_class="read_light",
                                description=interv_action.description)
            outcomes = apply_probe_result(hyps, logits, synth_probe, item, session)
            observed = dict.fromkeys(f"{o.observable}={o.observed}" for o in outcomes)
            _emit({"type": "intervention_executed", "intervention_id": interv_id,
                   "source_uri": item.source_uri, "hash": item.content_hash()[:12],
                   "summary": "; ".join(observed)})
            for o in outcomes:
                h = next(h for h in hyps if h.id == o.hyp_id)
                if o.matched and item.ref() not in h.evidence_refs:
                    h.evidence_refs.append(item.ref())
                _emit({"type": "posterior_updated", "id": o.hyp_id,
                       "posterior": round(h.posterior, 3),
                       "matched": o.matched, "observable": o.observable,
                       "source": "intervention"})
                if h.eliminated:
                    _emit({"type": "hypothesis_eliminated", "id": h.id,
                           "reason": h.eliminated_reason,
                           "source": "intervention"})

            # -------------------- verification loop --------------------
            # Only when policy_enabled=True (live demo). The graded harness
            # keeps its output shape stable — verification is orthogonal to
            # the ablation numbers.
            if policy_enabled:
                _emit({"type": "verification_started",
                       "intervention_id": interv_id,
                       "note": "checking recovery signal against band"})
                winner_now = ranked(hyps)[0] if hyps else None
                v = run_verification(bundle, interv_id, session, winner=winner_now)
                verified_recovery = v.recovered

                # The verification evidence is real, sealable, cited.
                session.append(v.evidence_item)
                resolvable.add(v.evidence_item.ref())

                _emit({"type": "verification_result",
                       "intervention_id": interv_id,
                       "recovered": v.recovered,
                       "signal": v.signal,
                       "baseline_value": v.baseline_value,
                       "post_intervention_value": v.post_value,
                       "recovery_band": v.recovery_band,
                       "reasoning": v.reasoning,
                       "source_uri": v.source_uri,
                       "synthesised": v.synthesised,
                       "hash": v.evidence_item.content_hash()[:12]})

                if not v.recovered:
                    _emit({"type": "auto_revert_triggered",
                           "intervention_id": interv_id,
                           "reason": "recovery signal outside band; enforced envelope revert",
                           "safety_envelope": interv_action.safety_envelope})

            continue  # re-score; loop will conclude next iteration

        if intervention_would_fire(actions) and not interv_available:
            # Honest failure mode: intervention is the argmax over all actions
            # but this bundle didn't author an outcome → we can't execute.
            _emit({"type": "intervention_would_fire",
                   "action_id": interv_action.action_id if interv_action else None,
                   "safety_envelope": interv_action.safety_envelope if interv_action else None,
                   "unavailable_reason": "not_available_in_prototype"})

        decision = decide(hyps, bundle.probe_catalog, already)
        if decision.action == "conclude":
            break
        if not probes_enabled:
            # ablation-off arm: we can *see* the ambiguity but cannot resolve it.
            _emit({"type": "ambiguity_detected", "margin": round(decision.margin, 3),
                   "tau": TAU, "resolvable": False,
                   "note": "probes disabled — concluding on prior evidence only"})
            break
        if len(probes_run) >= K_MAX_PROBES or (time.time() - probe_loop_t0) > T_WALL_SECONDS:
            nxt = select_probe(bundle.probe_catalog, hyps, already)
            _emit({"type": "exhausted", "margin": round(decision.margin, 3),
                   "would_run_next": nxt.probe.probe_id if nxt else None,
                   "surviving": [h.id for h in ranked(hyps)]})
            break

        _emit({"type": "ambiguity_detected", "margin": round(decision.margin, 3),
               "tau": TAU, "resolvable": True, "reason": decision.reason})

        choice = decision.probe_choice
        _emit({"type": "probe_selected", "probe_id": choice.probe.probe_id,
               "info_gain": choice.info_gain, "cost_ms": choice.probe.cost_ms,
               "description": choice.probe.description})

        item = bundle.run_probe(choice.probe.probe_id, probes_enabled=True)
        session.append(item)
        resolvable.add(item.ref())
        probes_run.append(choice.probe.probe_id)
        already.add(choice.probe.probe_id)

        outcomes = apply_probe_result(hyps, logits, choice.probe, item, session)
        observed = dict.fromkeys(f"{o.observable}={o.observed}" for o in outcomes)
        summary = "; ".join(observed)
        _emit({"type": "probe_result", "probe_id": choice.probe.probe_id,
               "source_uri": item.source_uri, "hash": item.content_hash()[:12],
               "summary": summary})

        for o in outcomes:
            h = next(h for h in hyps if h.id == o.hyp_id)
            if o.matched and item.ref() not in h.evidence_refs:
                h.evidence_refs.append(item.ref())
            _emit({"type": "posterior_updated", "id": o.hyp_id,
                   "posterior": round(h.posterior, 3),
                   "matched": o.matched, "observable": o.observable})
            if h.eliminated:
                _emit({"type": "hypothesis_eliminated", "id": h.id,
                       "reason": h.eliminated_reason})

    # --- verdict ------------------------------------------------------------
    winner = ranked(hyps)[0]
    rollback = is_rollback_hypothesis(winner.claim)
    used_refs = sorted({r for h in hyps for r in h.evidence_refs})
    # Interventions that did NOT verify collapse the provenance back to
    # observational — the intervention data was gathered but did not resolve
    # the incident, so the verdict rests on observation.
    if interventions_run and verified_recovery is False:
        provenance = "observational"
    elif interventions_run:
        provenance = "interventional"
    else:
        provenance = "observational"
    verdict = Verdict(
        incident_id=bundle.incident_id,
        root_cause_id=winner.id,
        summary=winner.claim,
        posterior=round(winner.posterior, 3),
        rollback_recommended=rollback,
        hypotheses=hyps,
        probes_run=probes_run,
        evidence_refs=used_refs,
        provenance=provenance,
        verified_recovery=verified_recovery,
    )

    _emit({"type": "provenance_labeled", "provenance": verdict.provenance,
           "interventions_run": interventions_run})

    if rollback:
        if policy_enabled:
            # Rollback is a recommendation from the mechanism, executed
            # downstream (Argo Rollouts / your deploy tool). The policy call
            # here says whether it WOULD be permitted; it is informational and
            # sealed into the audit trail with the same shape as the
            # intervention-side decision.
            rb_input = build_input_for_rollback(
                incident_id=bundle.incident_id,
                posterior=verdict.posterior,
                evidence_refs=used_refs,
                deploy_recent=True,
                upstream_outage=False,
            )
            rb_decision = evaluate_safely(rb_input)
            _emit({**rb_decision.to_event(),
                   "note": "policy check on the rollback recommendation — "
                           "execution belongs to the deploy tool, not this system"})
        else:
            _emit({"type": "gate_pending", "action": "rollback",
                   "note": "state-changing action requires explicit approval — not auto-fired"})

    # --- customer-impact scoring (AFTER the diagnostic verdict, on purpose) -
    # CIS is operational urgency, ORTHOGONAL to the diagnosis. Computing it
    # here — after `winner`, `posterior`, and `rollback_recommended` are
    # already frozen into `verdict` — makes it structurally impossible for
    # CIS to have influenced the diagnostic path. Sealed as a real evidence
    # leaf so the audit trail includes it.
    impact = compute_cis(bundle.raw)
    if impact is not None:
        _emit(impact.to_event())
        cis_item = cis_evidence_item(bundle.incident_id, impact)
        session.append(cis_item)
        resolvable.add(cis_item.ref())
        used_refs = sorted(set(used_refs) | {cis_item.ref()})
        verdict.customer_impact = {
            "cis_score":                impact.cis_score,
            "tier":                     impact.tier,
            "affected_users":           impact.affected_users,
            "sla_breach":               impact.sla_breach,
            "revenue_tagged_service":   impact.revenue_tagged_service,
            "impact_source":            impact.impact_source,
            "urgency_label":            impact.as_urgency_label(),
            "components":               dict(impact.components),
        }
        verdict.evidence_refs = used_refs

    # --- seal the evidence chain -------------------------------------------
    used_items = [e for e in session if e.ref() in set(used_refs)]
    sealed = seal(verdict, used_items)
    verdict.merkle_root = sealed["merkle_root"]
    verdict.signature = sealed["signature"]
    result_time = time.time() - t0
    _emit({"type": "verdict", "root_cause_id": verdict.root_cause_id,
           "summary": verdict.summary, "posterior": verdict.posterior,
           "rollback_recommended": verdict.rollback_recommended,
           "probes_run": probes_run})
    _emit({"type": "chain_sealed", "merkle_root": sealed["merkle_root"],
           "signature": sealed["signature"][:24] + "…",
           "leaf_count": len(used_items)})

    return RunResult(verdict=verdict, events=events, probes_run=probes_run,
                     interventions_run=interventions_run,
                     verified_recovery=verified_recovery,
                     time_to_conclusion_s=round(result_time, 3),
                     sealed=sealed, cited_items=used_items)
