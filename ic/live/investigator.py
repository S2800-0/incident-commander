"""Autonomous investigation over live telemetry.

    prior evidence (deploy log, golden-signal series, dependency series, runbooks)
      → generate hypotheses from that context
      → the three existing agents score them (unchanged replay lenses)
      → loop: score probes by EIG / cost → run one → Bayesian update
      → stop when a leader is confident AND positively confirmed,
        or no probe can still move beliefs → diagnose or ABSTAIN

Stopping rules (mode="eig"):
  1. leader ≥ CONCLUDE and its signature prediction was observed → diagnose (positive)
  2. leader ≥ CONCLUDE but only won by exclusion → run a probe that tests the
     leader's own signature if one is available (guards against an incomplete
     hypothesis space: the leader may simply be the last one standing)
  3. otherwise run the probe with the highest EIG per second, if EIG ≥ MIN_EIG
  4. nothing informative left → diagnose by exclusion if leader ≥ CONCLUDE,
     else ABSTAIN and escalate

Baseline modes, for measurement only:
  exhaustive — run every applicable probe, cheapest first, then apply rules 1/4
  no_probes  — act on the agents' prior leader with no investigation
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..agents import change_agent, history_agent, telemetry_agent
from ..models import EvidenceItem
from ..orchestrator import NO_EVIDENCE_PENALTY
from . import beliefs
from .catalog import (CANARY_DURATION_S, CANARY_PCT, OBSERVABLES, PROBES, LiveHypothesis,
                      ProbeSpec, generate_hypotheses, runbook_evidence)
from .governance import authorize
from .probes import BASELINE_WINDOW, ProbeOutput, cohort_probe, dependency_probe, iso
from .target import TargetClient
from .telemetry import TelemetryStore

CONCLUDE = 0.90
MIN_EIG = 0.05          # nats
RULED_OUT = 0.02
# A probe is wasted when it barely moved the beliefs: KL(after ‖ before) below this.
# KL, not entropy reduction — a probe that correctly REVEALS ambiguity raises
# entropy yet is highly informative (it can stop the agent acting on a guess).
WASTE_NATS = 0.01
K_MAX_PROBES = 5
T_WALL_S = 90.0
PRIOR_LOOKBACK_S = 60.0
SERIES_STEP_S = 3.0
MODES = ("eig", "exhaustive", "no_probes")

Emit = Callable[[dict], None]


@dataclass
class LiveContext:
    """Duck-types the parts of ic.bundle.Bundle the agents read."""
    incident_id: str
    alert: dict
    fired_at: float
    service: str
    raw: dict = field(default_factory=dict)
    topology: dict = field(default_factory=dict)


@dataclass
class ProbeRecord:
    probe_id: str
    observable: str
    reason: str
    eig_nats: float
    cost_ms: int
    outcome: Optional[str]
    duration_ms: float
    entropy_before: float
    entropy_after: float
    leader_before: str
    leader_after: str
    interventional: bool
    evidence_ref: Optional[str]
    blocked_by_policy: bool = False
    kl_nats: float = 0.0          # KL(posterior after ‖ posterior before): information actually gained

    @property
    def realized_gain(self) -> float:
        return self.kl_nats

    @property
    def wasted(self) -> bool:
        return self.outcome is None or self.kl_nats < WASTE_NATS


@dataclass
class InvestigationResult:
    hypotheses: list[LiveHypothesis]
    posteriors: dict[str, float]
    leader: Optional[LiveHypothesis]
    abstained: bool
    abstain_reason: str
    confirmation: str                      # positive | exclusion_only | none
    probes: list[ProbeRecord]
    evidence: list[EvidenceItem]
    policy_leaves: list[EvidenceItem]
    policy_decisions: list[dict]
    actions: list[dict]
    entropy_trace: list[float]
    last_release: Optional[dict]
    deploy_recent: bool
    started_at: float
    ended_at: float
    stop_reason: str
    next_policy_seq: int


def _ts(iso_str: str) -> float:
    from ..reasoner import parse_ts
    return parse_ts(iso_str).timestamp()


def gather_prior_evidence(ctx: LiveContext, store: TelemetryStore, target: TargetClient,
                          dependencies: list[str]) -> tuple[list[EvidenceItem], Optional[dict]]:
    now = time.time()
    t0 = now - PRIOR_LOOKBACK_S
    items: list[EvidenceItem] = []

    releases = [d for d in target.deployments(limit=10) if d["kind"] in ("code", "config")]
    last_release = releases[-1] if releases else None
    if last_release:
        items.append(EvidenceItem(
            source_type="deployment",
            source_uri=f"deploy://{ctx.service}/deployments/{last_release['version']}",
            retrieved_at=iso(now), payload=dict(last_release)))

    def err_pct(a: float, b: float) -> Optional[float]:
        s = store.request_stats(a, b)
        return None if s["error_rate"] is None else s["error_rate"] * 100

    def p95(a: float, b: float) -> Optional[float]:
        return store.request_stats(a, b)["p95_ms"]

    items.append(EvidenceItem(
        source_type="metric", source_uri=f"otlp://{ctx.service}/metric/http.server.5xx_rate",
        retrieved_at=iso(now), measures=["http_5xx_rate"],
        payload={"unit": "pct", "series": store.series(err_pct, t0, now, SERIES_STEP_S)}))
    items.append(EvidenceItem(
        source_type="metric", source_uri=f"otlp://{ctx.service}/metric/http.server.request.duration.p95",
        retrieved_at=iso(now), measures=["http_p95_latency"],
        payload={"unit": "ms", "series": store.series(p95, t0, now, SERIES_STEP_S)}))
    for dep in dependencies:
        def dep_p95(a: float, b: float, dep=dep) -> Optional[float]:
            return store.dependency_stats(dep, a, b)["p95_ms"]
        items.append(EvidenceItem(
            source_type="metric", source_uri=f"otlp://{dep}/client-observed/latency_p95",
            retrieved_at=iso(now), measures=[f"{dep}_latency_p95"],
            payload={"unit": "ms", "observed_by": ctx.service,
                     "series": store.series(dep_p95, t0, now, SERIES_STEP_S)}))
    items.extend(runbook_evidence())
    return items, last_release


def investigate(ctx: LiveContext, store: TelemetryStore, target: TargetClient, emit: Emit,
                mode: str = "eig", dependencies: Optional[list[str]] = None) -> InvestigationResult:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    started = time.time()
    dependencies = dependencies or ["orders-db", "inventory-service"]

    evidence, last_release = gather_prior_evidence(ctx, store, target, dependencies)
    resolvable = {e.ref() for e in evidence}
    for e in evidence:
        if e.source_type in ("deployment", "metric"):
            emit({"type": "evidence_collected", "source_uri": e.source_uri, "source_type": e.source_type,
                  "stage": "prior", "hash": e.content_hash()[:12],
                  "points": len(e.payload.get("series", [])) if "series" in e.payload else None})

    release_ts = _ts(last_release["deployed_at"]) if last_release else None
    lhyps = generate_hypotheses(ctx.service, ctx.fired_at, last_release, release_ts, dependencies)
    deploy_recent = any(h.kind == "deploy" for h in lhyps)
    by_id = {h.id: h for h in lhyps}
    hyps = [h.hyp for h in lhyps]
    for h in lhyps:
        emit({"type": "hypothesis_generated", "id": h.id, "claim": h.hyp.claim, "kind": h.kind,
              "remediation": h.remediation, "generated_by": "live context templates",
              "predictions": [{"observable": po.observable, "prediction": po.prediction,
                               "role": h.roles.get(po.observable)} for po in h.hyp.predicted_observations]})

    # --- agent fan-out: the replay lenses, unchanged, over live evidence ---
    logits = {h.id: 0.0 for h in hyps}
    for mod in (change_agent, telemetry_agent, history_agent):
        emit({"type": "agent_started", "agent_id": mod.AGENT_ID, "mode": "analytical"})
        for c in mod.assess(ctx, evidence, hyps):
            good = [r for r in c.evidence_refs if r in resolvable]
            logits[c.hyp_id] += c.logit_delta
            h = by_id[c.hyp_id].hyp
            for r in good:
                if r not in h.evidence_refs:
                    h.evidence_refs.append(r)
            emit({"type": "agent_reasoned", "agent_id": c.agent_id, "hyp_id": c.hyp_id,
                  "delta": round(c.logit_delta, 3), "rationale": c.rationale, "evidence_refs": good})
    for h in hyps:
        if not h.evidence_refs:
            logits[h.id] -= NO_EVIDENCE_PENALTY

    posteriors = beliefs.softmax(logits) if hyps else {}
    entropy_trace = [beliefs.entropy(posteriors)]
    emit({"type": "beliefs_updated", "posteriors": _rounded(posteriors),
          "entropy_nats": round(entropy_trace[-1], 4), "source": "prior — agent fan-out"})

    probes: list[ProbeRecord] = []
    policy_leaves: list[EvidenceItem] = []
    policy_decisions: list[dict] = []
    actions: list[dict] = []
    confirmed: set[str] = set()
    ran: set[str] = set()
    unavailable: dict[str, str] = {}
    policy_seq = 1
    stop_reason = ""

    state = target.state()
    caps = state.get("capabilities", {})
    if not caps.get("cohort_routing"):
        unavailable["P_version_cohorts"] = caps.get("cohort_routing_reason", "cohort routing unavailable")

    def leader_id() -> Optional[str]:
        return max(posteriors, key=posteriors.get) if posteriors else None

    def predictions_for(spec: ProbeSpec) -> dict[str, Optional[str]]:
        return {h.id: h.prediction(spec.observable) for h in lhyps}

    while mode != "no_probes" and hyps:
        candidates = [p for p in PROBES if p.probe_id not in ran and p.probe_id not in unavailable]
        scored = []
        for p in candidates:
            eig = beliefs.expected_information_gain(posteriors, predictions_for(p), OBSERVABLES[p.observable])
            scored.append((p, eig, eig / (p.cost_ms / 1000.0)))
        emit({"type": "probes_scored", "mode": mode,
              "candidates": [{"probe_id": p.probe_id, "description": p.description, "eig_nats": round(e, 4),
                              "cost_ms": p.cost_ms, "score": round(s, 4), "interventional": p.interventional}
                             for p, e, s in sorted(scored, key=lambda x: -x[2])],
              "unavailable": unavailable})

        lid = leader_id()
        top = posteriors[lid]
        choice: Optional[tuple[ProbeSpec, float]] = None
        reason = ""
        if mode == "exhaustive":
            remaining = sorted(scored, key=lambda x: x[0].cost_ms)
            if remaining:
                choice, reason = (remaining[0][0], remaining[0][1]), "exhaustive baseline — every applicable probe"
        else:
            if top >= CONCLUDE and lid in confirmed:
                stop_reason = f"{lid} at {top:.2f} with positive signature evidence"
                break
            if top >= CONCLUDE:
                sig = by_id[lid].signature()
                conf = [(p, e) for p, e, _ in scored if p.observable in sig]
                if conf:
                    choice, reason = conf[0], (f"{lid} leads at {top:.2f} only by exclusion — "
                                               "testing its own signature before acting")
            if choice is None:
                informative = [x for x in scored if x[1] >= MIN_EIG]
                if informative:
                    best = max(informative, key=lambda x: x[2])
                    choice, reason = (best[0], best[1]), "highest expected information gain per second"
        if choice is None:
            stop_reason = stop_reason or "no remaining probe can change the beliefs (EIG below threshold)"
            break
        if len(probes) >= K_MAX_PROBES or time.time() - started > T_WALL_S:
            stop_reason = "investigation budget exhausted"
            emit({"type": "budget_exhausted", "probes": len(probes), "elapsed_s": round(time.time() - started, 1)})
            break

        spec, eig = choice
        ran.add(spec.probe_id)
        emit({"type": "probe_selected", "probe_id": spec.probe_id, "description": spec.description,
              "eig_nats": round(eig, 4), "cost_ms": spec.cost_ms, "reason": reason,
              "interventional": spec.interventional})

        h_before, l_before, p_before = beliefs.entropy(posteriors), lid, dict(posteriors)
        out: Optional[ProbeOutput] = None
        blocked = False
        if spec.interventional:
            refs = sorted({r for h in hyps for r in h.evidence_refs} | {e.ref() for e in evidence if e.source_type == "deployment"})
            auth = authorize(ctx.incident_id, policy_seq, "canary_probe", evidence_refs=refs,
                             reversible=True, blast_radius="service", posterior=top,
                             safety_envelope={"max_traffic_pct": CANARY_PCT, "max_duration_s": CANARY_DURATION_S,
                                              "auto_revert": True},
                             extra={"probe_id": spec.probe_id})
            policy_seq += 1
            policy_leaves.append(auth.evidence)
            policy_decisions.append(_decision_row("canary_probe", auth))
            emit({"type": "policy_decision", "action": "canary_probe", "allow": auth.allow,
                  "reasons": auth.decision.reasons, "engine_available": auth.decision.engine_available,
                  "stage": "investigation", "source_uri": auth.evidence.source_uri})
            if not auth.allow:
                blocked = True
                unavailable[spec.probe_id] = "denied by policy"
                emit({"type": "action_blocked", "action": "canary_probe", "reasons": auth.decision.reasons,
                      "note": "interventional probe not executed"})
            else:
                emit({"type": "action_started", "action": "canary_probe",
                      "detail": f"routing {CANARY_PCT:g}% of traffic to {state['previous_version']} "
                                f"for {CANARY_DURATION_S:g}s"})
                out = cohort_probe(spec, store, target, ctx.incident_id,
                                   state["active_version"], state["previous_version"])
                actions.extend(out.actions)
        else:
            out = dependency_probe(spec, store, ctx.incident_id, ctx.fired_at)

        if out is not None:
            evidence.append(out.evidence)
            resolvable.add(out.evidence.ref())
            if out.outcome is not None:
                preds = predictions_for(spec)
                posteriors = beliefs.update(posteriors, preds, out.outcome, OBSERVABLES[spec.observable])
                for h in lhyps:
                    if preds[h.id] == out.outcome:
                        if out.evidence.ref() not in h.hyp.evidence_refs:
                            h.hyp.evidence_refs.append(out.evidence.ref())
                        if h.roles.get(spec.observable) == "signature":
                            confirmed.add(h.id)
        entropy_trace.append(beliefs.entropy(posteriors))
        rec = ProbeRecord(spec.probe_id, spec.observable, reason, round(eig, 4), spec.cost_ms,
                          out.outcome if out else None, round(out.duration_ms, 1) if out else 0.0,
                          h_before, entropy_trace[-1], l_before, leader_id(), spec.interventional,
                          out.evidence.ref() if out else None, blocked,
                          round(beliefs.kl_divergence(posteriors, p_before), 4))
        probes.append(rec)
        emit({"type": "probe_executed", "probe_id": spec.probe_id, "outcome": rec.outcome,
              "stats": out.stats if out else None, "duration_ms": rec.duration_ms,
              "hash": out.evidence.content_hash()[:12] if out else None,
              "realized_gain_nats": round(rec.realized_gain, 4), "wasted": rec.wasted,
              "blocked_by_policy": blocked})
        emit({"type": "beliefs_updated", "posteriors": _rounded(posteriors),
              "entropy_nats": round(entropy_trace[-1], 4), "source": spec.probe_id,
              "confirmed": sorted(confirmed)})
        for h in lhyps:
            if posteriors[h.id] < RULED_OUT and not h.hyp.eliminated:
                h.hyp.eliminated = True
                h.hyp.eliminated_reason = (f"{spec.probe_id} observed {spec.observable}={rec.outcome}, "
                                           f"predicted {h.prediction(spec.observable)}")
                emit({"type": "hypothesis_ruled_out", "id": h.id, "posterior": round(posteriors[h.id], 4),
                      "reason": h.hyp.eliminated_reason})

    for h in lhyps:
        h.hyp.posterior = round(posteriors.get(h.id, 0.0), 4)

    lid = leader_id()
    leader = by_id.get(lid) if lid else None
    top = posteriors.get(lid, 0.0) if lid else 0.0
    abstained, abstain_reason, confirmation = False, "", "none"

    if not hyps:
        abstained, abstain_reason = True, "no hypothesis could be generated from the observed context"
    elif mode == "no_probes":
        stop_reason = "baseline: act on the prior leader without investigating"
        confirmation = "none"
    elif top >= CONCLUDE:
        confirmation = "positive" if lid in confirmed else "exclusion_only"
    else:
        abstained = True
        surviving = {h: round(p, 3) for h, p in posteriors.items() if p >= 0.10}
        abstain_reason = (f"evidence cannot separate the remaining explanations {surviving}; "
                          f"leader {lid} at {top:.2f} < {CONCLUDE}")

    if abstained:
        emit({"type": "abstained", "reason": abstain_reason, "posteriors": _rounded(posteriors),
              "stop_reason": stop_reason})
    else:
        emit({"type": "diagnosis", "root_cause_id": lid, "claim": leader.hyp.claim, "posterior": round(top, 4),
              "confirmation": confirmation, "stop_reason": stop_reason})

    return InvestigationResult(
        hypotheses=lhyps, posteriors=posteriors, leader=None if abstained else leader,
        abstained=abstained, abstain_reason=abstain_reason, confirmation=confirmation,
        probes=probes, evidence=evidence, policy_leaves=policy_leaves,
        policy_decisions=policy_decisions, actions=actions, entropy_trace=entropy_trace,
        last_release=last_release, deploy_recent=deploy_recent, started_at=started,
        ended_at=time.time(), stop_reason=stop_reason, next_policy_seq=policy_seq)


def _rounded(p: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 4) for k, v in p.items()}


def _decision_row(action: str, auth) -> dict:
    return {"action": action, "allow": auth.allow, "reasons": sorted(auth.decision.reasons),
            "engine_available": auth.decision.engine_available, "source_uri": auth.evidence.source_uri}
