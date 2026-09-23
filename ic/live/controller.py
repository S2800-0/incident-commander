"""LiveController — the autonomous incident lifecycle.

    detect (SLO breach from live telemetry)
      → investigate (ic/live/investigator.py)
      → abstain ─────────────────────────────────────▶ ESCALATED_INSUFFICIENT_EVIDENCE
      → propose remediation → OPA ── DENY ───────────▶ ESCALATED_POLICY_DENIED
                                 └─ ALLOW → execute against the real service
      → verify from fresh telemetry ── recovered ────▶ RESOLVED
                                    └─ not recovered → OPA(auto_revert) → revert
                                                     ─▶ ESCALATED_REMEDIATION_FAILED
      → customer impact (measured) → seal every leaf → signed postmortem

No step waits for a human. Humans receive escalations; they do not unblock the
normal path.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Optional

from ..cis import compute as compute_cis
from ..cis import to_evidence_item as cis_evidence_item
from ..evidence_chain import seal, verify_postmortem, write_postmortem
from ..models import EvidenceItem, Verdict
from ..policy import evaluate_safely
from .catalog import REMEDIATIONS
from .detector import Breach, Detector
from .governance import authorize
from .investigator import MODES, LiveContext, investigate
from .probes import iso
from .target import TargetClient, TargetError
from .telemetry import TelemetryStore
from .verify import verify_recovery

SERVICE = "checkout-service"
RESULTS_DIR = Path(os.environ.get("IC_LIVE_RESULTS_DIR", "live_runs"))
EVENT_CAP = 20000


class LiveController:
    def __init__(self, store: TelemetryStore, target: Optional[TargetClient] = None,
                 mode: Optional[str] = None) -> None:
        self.store = store
        self.target = target or TargetClient()
        self.detector = Detector(store)
        self.mode = mode or os.environ.get("IC_LIVE_MODE", "eig")
        self.events: deque[dict] = deque(maxlen=EVENT_CAP)
        self.incidents: list[dict] = []
        self.active: Optional[dict] = None
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.opa_available: Optional[bool] = None
        self._opa_checked_at = 0.0
        RESULTS_DIR.mkdir(exist_ok=True)
        self._incident_counter = self._load_counter()

    # --- plumbing -----------------------------------------------------------------

    def _load_counter(self) -> int:
        f = RESULTS_DIR / "incidents.jsonl"
        if not f.exists():
            return 0
        return sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip())

    def emit(self, e: dict) -> None:
        with self._lock:
            self._seq += 1
            e = {"seq": self._seq, "ts": time.time(),
                 "incident_id": self.active["incident_id"] if self.active else None, **e}
            self.events.append(e)
            if self.active is not None:
                self.active["stage"] = _stage_for(e["type"], self.active.get("stage"))

    def events_since(self, seq: int, limit: int = 500) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e["seq"] > seq][:limit]

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.emit({"type": "mode_changed", "mode": mode})

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="ic-live-controller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _refresh_opa(self) -> None:
        if time.time() - self._opa_checked_at < 10:
            return
        self._opa_checked_at = time.time()
        d = evaluate_safely({"mode": "live", "action": "auto_revert", "reverts_agent_action": True,
                             "evidence_refs": ["healthcheck://opa"], "reversible": True,
                             "blast_radius": "service"})
        self.opa_available = d.engine_available

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            active = dict(self.active) if self.active else None
            recent = [_summary(r) for r in self.incidents[-25:]][::-1]
        return {
            "mode": self.mode,
            "armed": self.detector.armed,
            "investigating": active is not None,
            "active_incident": active,
            "opa_available": self.opa_available,
            "telemetry": {"batches": self.store.ingested_batches,
                          "ingest_lag_p50_s": (round(sorted(self.store.ingest_lag_s)[len(self.store.ingest_lag_s) // 2], 3)
                                               if self.store.ingest_lag_s else None),
                          "last_ingest_age_s": (round(now - self.store.last_ingest_at, 1)
                                                if self.store.last_ingest_at else None)},
            "service_health": self.store.request_stats(now - 5, now),
            "slo": self.detector.slo.__dict__,
            "incidents": recent,
            "last_seq": self._seq,
        }

    # --- detection loop -------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_opa()
                breach = self.detector.evaluate()
                if breach is not None:
                    self.handle(breach)
            except Exception:  # the loop must survive anything a single incident does
                traceback.print_exc()
            self._stop.wait(1.0)

    # --- one incident ------------------------------------------------------------------

    def handle(self, breach: Breach) -> dict:
        self._incident_counter += 1
        iid = f"LIVE-{self._incident_counter:04d}"
        detected_at = time.time()
        alert = breach.alert(SERVICE)
        rec: dict = {"incident_id": iid, "mode": self.mode, "status": "INVESTIGATING", "stage": "detected",
                     "detected_at": detected_at, "fired_at": breach.fired_at, "alert": alert,
                     "breach": breach.__dict__}
        with self._lock:
            self.active = rec
        self.emit({"type": "incident_detected", "alert": alert, "value": breach.value,
                   "threshold": breach.threshold, "requests": breach.requests, "mode": self.mode,
                   "autonomy": "no human approval in the normal path; OPA decides every action"})
        try:
            self._run(rec, breach)
        except Exception as e:
            rec["status"] = "ERROR"
            rec["error"] = f"{type(e).__name__}: {e}"
            self.emit({"type": "incident_error", "error": rec["error"]})
            traceback.print_exc()
        finally:
            rec["closed_at"] = time.time()
            rec["time_to_close_s"] = round(rec["closed_at"] - detected_at, 2)
            self.emit({"type": "incident_closed", "status": rec["status"],
                       "time_to_close_s": rec["time_to_close_s"], "summary": _summary(rec)})
            with self._lock:
                self.incidents.append(rec)
                self.active = None
            with open(RESULTS_DIR / "incidents.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        return rec

    def _run(self, rec: dict, breach: Breach) -> None:
        iid = rec["incident_id"]
        ctx = LiveContext(incident_id=iid, alert=rec["alert"], fired_at=breach.fired_at, service=SERVICE,
                          topology={"nodes": [SERVICE, "orders-db", "inventory-service", "payment-gateway"]})

        inv = investigate(ctx, self.store, self.target, self.emit, mode=self.mode)
        leaves: list[EvidenceItem] = list(inv.evidence) + list(inv.policy_leaves)
        policy_rows = list(inv.policy_decisions)
        seq = inv.next_policy_seq
        rec.update({
            "investigation_duration_s": round(inv.ended_at - inv.started_at, 2),
            "hypotheses": [{"id": h.id, "claim": h.hyp.claim, "posterior": h.hyp.posterior, "kind": h.kind}
                           for h in inv.hypotheses],
            "probes": [{**p.__dict__, "realized_gain_nats": round(p.realized_gain, 4), "wasted": p.wasted}
                       for p in inv.probes],
            "probes_run": len(inv.probes),
            "probes_wasted": sum(p.wasted for p in inv.probes),
            "interventional_probes": sum(1 for p in inv.probes if p.interventional and not p.blocked_by_policy),
            "canary_exposure_pct_s": sum(a["pct"] * a["duration_s"] for a in inv.actions if a["action"] == "canary"),
            "entropy_trace": [round(h, 4) for h in inv.entropy_trace],
            "stop_reason": inv.stop_reason,
            "abstained": inv.abstained,
            "diagnosis": (None if inv.leader is None else
                          {"root_cause_id": inv.leader.id, "claim": inv.leader.hyp.claim,
                           "posterior": inv.leader.hyp.posterior, "confirmation": inv.confirmation}),
            "remediation": None, "verification": None, "auto_revert": None,
        })
        refs = sorted({e.ref() for e in inv.evidence})
        verified: Optional[bool] = None

        if inv.abstained:
            rec["status"] = "ESCALATED_INSUFFICIENT_EVIDENCE"
            self.emit({"type": "escalated", "reason": inv.abstain_reason,
                       "handoff": "on-call SRE paged with the sealed evidence trail; no action taken"})
        else:
            leader = inv.leader
            spec = REMEDIATIONS[leader.remediation]
            self.emit({"type": "remediation_proposed", "action": spec.action, "description": spec.description,
                       "reversible": spec.reversible, "blast_radius": spec.blast_radius,
                       "for_hypothesis": leader.id, "confirmation": inv.confirmation})
            auth = authorize(iid, seq, spec.action, evidence_refs=refs, reversible=spec.reversible,
                             blast_radius=spec.blast_radius, posterior=leader.hyp.posterior,
                             deploy_recent=inv.deploy_recent, upstream_outage=False,
                             confirmation=inv.confirmation, verification_planned=True,
                             safety_envelope={"auto_revert": spec.reversible, "verification_window_s": 6})
            seq += 1
            leaves.append(auth.evidence)
            policy_rows.append({"action": spec.action, "allow": auth.allow,
                                "reasons": sorted(auth.decision.reasons),
                                "engine_available": auth.decision.engine_available,
                                "source_uri": auth.evidence.source_uri})
            self.emit({"type": "policy_decision", "action": spec.action, "allow": auth.allow,
                       "reasons": auth.decision.reasons, "engine_available": auth.decision.engine_available,
                       "stage": "remediation", "source_uri": auth.evidence.source_uri})

            if not auth.allow:
                rec["status"] = ("ESCALATED_POLICY_ENGINE_UNAVAILABLE" if not auth.decision.engine_available
                                 else "ESCALATED_POLICY_DENIED")
                rec["remediation"] = {"action": spec.action, "executed": False, "blocked_by_policy": True,
                                      "reasons": sorted(auth.decision.reasons)}
                self.emit({"type": "action_blocked", "action": spec.action, "reasons": auth.decision.reasons,
                           "note": "policy denied — nothing was executed against the service"})
                self.emit({"type": "escalated", "reason": f"{spec.action} denied by policy: "
                                                          f"{', '.join(sorted(auth.decision.reasons))}",
                           "handoff": f"diagnosis {leader.id} handed to the owning team with evidence"})
            else:
                verified = self._remediate(rec, iid, spec, leader, leaves, policy_rows, seq)

        rec["policy_decisions"] = policy_rows
        rec["policy_denies"] = sum(1 for p in policy_rows if not p["allow"])

        # --- measured customer impact (after the outcome is known; never fed back) ---
        closed = time.time()
        impact_stats = self.store.request_stats(breach.fired_at - self.detector.slo.window_s, closed)
        block = {"tier": "P1", "affected_users": impact_stats["errors"], "sla_breach": True,
                 "revenue_tagged_service": True,
                 "impact_source": f"measured: {impact_stats['errors']} failed checkout requests of "
                                  f"{impact_stats['requests']} between detection and close "
                                  "(failed requests, not unique users)"}
        impact = compute_cis({"customer_impact": block})
        if impact is not None:
            leaves.append(cis_evidence_item(iid, impact))
            rec["customer_impact"] = {"cis_score": impact.cis_score, "failed_requests": impact_stats["errors"],
                                      "requests": impact_stats["requests"], "urgency": impact.as_urgency_label()}
            self.emit({**impact.to_event(), "failed_requests": impact_stats["errors"],
                       "requests": impact_stats["requests"]})

        self._seal(rec, inv, leaves, verified)

    def _remediate(self, rec, iid, spec, leader, leaves, policy_rows, seq) -> Optional[bool]:
        reason = f"{leader.id} at {leader.hyp.posterior:.2f} ({rec['diagnosis']['confirmation']})"
        before = self.target.state()
        self.emit({"type": "action_started", "action": spec.action, "detail": spec.description})
        try:
            resp = self.target.execute(spec.action, iid, reason)
        except TargetError as e:
            rec["status"] = "ESCALATED_ACTION_FAILED"
            rec["remediation"] = {"action": spec.action, "executed": False, "error": str(e)}
            self.emit({"type": "action_failed", "action": spec.action, "error": str(e)})
            return None
        action_at = time.time()
        after = self.target.state()
        leaves.append(EvidenceItem(
            source_type="action_execution", source_uri=f"target://{SERVICE}/ops/{spec.action}/{iid}",
            retrieved_at=iso(action_at),
            payload={"action": spec.action, "reason": reason, "http_status": resp["http_status"],
                     "response": resp["body"], "state_before": before, "state_after": after}))
        rec["remediation"] = {"action": spec.action, "executed": True, "executed_at": action_at,
                              "version_before": before.get("active_version"),
                              "version_after": after.get("active_version")}
        rec["time_to_mitigation_action_s"] = round(action_at - rec["detected_at"], 2)
        self.emit({"type": "remediation_executed", "action": spec.action,
                   "version_before": before.get("active_version"), "version_after": after.get("active_version"),
                   "state_after": after})

        self.emit({"type": "verification_started", "window": "2s settle + 6s of fresh telemetry"})
        ver = verify_recovery(self.store, iid, action_at, "post-remediation", self.detector.slo)
        leaves.append(ver.evidence)
        rec["verification"] = {"recovered": ver.recovered, "after": ver.stats["after"],
                               "before": ver.stats["before"], "checks": ver.stats["checks"]}
        self.emit({"type": "verification_result", "recovered": ver.recovered, **ver.stats,
                   "hash": ver.evidence.content_hash()[:12]})

        if ver.recovered:
            rec["status"] = "RESOLVED"
            rec["time_to_verified_recovery_s"] = round(time.time() - rec["detected_at"], 2)
            return True

        # --- auto-revert, itself policy-gated ---
        self.emit({"type": "auto_revert_triggered", "action": spec.action,
                   "reason": "fresh telemetry did not recover — undoing the agent's change"})
        refs = sorted({e.ref() for e in leaves if e.source_type in ("verification", "action_execution")})
        auth = authorize(iid, seq, "auto_revert", evidence_refs=refs, reversible=True, blast_radius="service",
                         reverts_agent_action=True, extra={"reverts": spec.action})
        leaves.append(auth.evidence)
        policy_rows.append({"action": "auto_revert", "allow": auth.allow, "reasons": sorted(auth.decision.reasons),
                            "engine_available": auth.decision.engine_available,
                            "source_uri": auth.evidence.source_uri})
        self.emit({"type": "policy_decision", "action": "auto_revert", "allow": auth.allow,
                   "reasons": auth.decision.reasons, "engine_available": auth.decision.engine_available,
                   "stage": "auto_revert", "source_uri": auth.evidence.source_uri})
        rec["status"] = "ESCALATED_REMEDIATION_FAILED"
        if not auth.allow:
            rec["auto_revert"] = {"executed": False, "reasons": sorted(auth.decision.reasons)}
            self.emit({"type": "action_blocked", "action": "auto_revert", "reasons": auth.decision.reasons})
        else:
            resp = self.target.revert(spec.action, iid, before, "verification failed")
            reverted = self.target.state()
            leaves.append(EvidenceItem(
                source_type="action_execution", source_uri=f"target://{SERVICE}/ops/auto_revert/{iid}",
                retrieved_at=iso(time.time()),
                payload={"action": "auto_revert", "reverts": spec.action, "http_status": resp["http_status"],
                         "response": resp["body"], "state_after": reverted,
                         "restored_pre_action_version": reverted.get("active_version") == before.get("active_version")}))
            rec["auto_revert"] = {"executed": True, "version_after": reverted.get("active_version"),
                                  "restored": reverted.get("active_version") == before.get("active_version")}
            self.emit({"type": "auto_revert_executed", "action": spec.action,
                       "version_after": reverted.get("active_version"),
                       "restored": rec["auto_revert"]["restored"]})
        self.emit({"type": "escalated",
                   "reason": f"{spec.action} executed but the service did not recover — diagnosis "
                             f"{leader.id} is contradicted by fresh telemetry; change reverted",
                   "handoff": "on-call SRE paged: cause lies outside the generated hypotheses"})
        return False

    def _seal(self, rec: dict, inv, leaves: list[EvidenceItem], verified: Optional[bool]) -> None:
        iid = rec["incident_id"]
        uniq: dict[str, EvidenceItem] = {}
        for it in leaves:
            uniq.setdefault(it.source_uri, it)
        items = list(uniq.values())
        leader = inv.leader
        executed = bool(rec.get("remediation") and rec["remediation"].get("executed")) or rec["interventional_probes"] > 0
        verdict = Verdict(
            incident_id=iid,
            root_cause_id=leader.id if leader else "UNDETERMINED",
            summary=(leader.hyp.claim if leader else f"ABSTAINED — {inv.abstain_reason}") + f" [status: {rec['status']}]",
            posterior=(leader.hyp.posterior if leader else round(max(inv.posteriors.values(), default=0.0), 4)),
            rollback_recommended=bool(leader and leader.remediation == "rollback_deploy"),
            hypotheses=[h.hyp for h in inv.hypotheses],
            probes_run=[p.probe_id for p in inv.probes],
            evidence_refs=sorted(uniq),
            provenance="interventional" if executed else "observational",
            verified_recovery=verified,
            customer_impact=rec.get("customer_impact"),
        )
        sealed = seal(verdict, items)
        path = RESULTS_DIR / f"{iid}.postmortem.json"
        write_postmortem(verdict, items, str(path), sealed=sealed)
        ok, msgs = verify_postmortem(json.loads(path.read_text()))
        rec["chain"] = {"merkle_root": sealed["merkle_root"], "leaves": len(items), "verified": ok,
                        "postmortem": str(path),
                        "leaf_types": sorted({i.source_type for i in items})}
        self.emit({"type": "chain_sealed", "merkle_root": sealed["merkle_root"], "leaves": len(items),
                   "verified": ok, "leaf_types": rec["chain"]["leaf_types"], "postmortem": str(path)})


# --- helpers ---------------------------------------------------------------------------

_STAGES = {
    "incident_detected": "detected", "evidence_collected": "investigating", "hypothesis_generated": "investigating",
    "agent_started": "investigating", "probes_scored": "investigating", "probe_selected": "investigating",
    "probe_executed": "investigating", "diagnosis": "diagnosed", "abstained": "abstained",
    "remediation_proposed": "policy", "policy_decision": None, "action_blocked": "blocked",
    "remediation_executed": "remediated", "verification_started": "verifying",
    "verification_result": "verified", "auto_revert_triggered": "reverting", "auto_revert_executed": "reverted",
    "escalated": "escalated", "chain_sealed": "sealed",
}


def _stage_for(event_type: str, current: Optional[str]) -> Optional[str]:
    s = _STAGES.get(event_type)
    return s or current


def _summary(r: dict) -> dict:
    diag = r.get("diagnosis") or {}
    return {
        "incident_id": r["incident_id"], "status": r.get("status"), "mode": r.get("mode"),
        "detected_at": r.get("detected_at"), "time_to_close_s": r.get("time_to_close_s"),
        "investigation_duration_s": r.get("investigation_duration_s"),
        "probes_run": r.get("probes_run"), "probes_wasted": r.get("probes_wasted"),
        "root_cause_id": diag.get("root_cause_id"), "confirmation": diag.get("confirmation"),
        "remediation": (r.get("remediation") or {}).get("action"),
        "remediation_executed": (r.get("remediation") or {}).get("executed"),
        "recovered": (r.get("verification") or {}).get("recovered"),
        "auto_reverted": (r.get("auto_revert") or {}).get("executed"),
        "policy_denies": r.get("policy_denies"),
        "chain_verified": (r.get("chain") or {}).get("verified"),
    }
