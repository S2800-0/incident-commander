"""What live mode can hypothesise, measure, and do.

Hypotheses are GENERATED per incident from observed context — which release
landed and when, which dependencies are instrumented — not read from a bundle.
The generator is rule-based (template instantiation), not an LLM: every
hypothesis it can produce is enumerable here, which also means a cause outside
these templates is invisible to the agent. The verification step exists
precisely because of that limit.

Each prediction carries a role:
  * signature — what this cause specifically implies (positive evidence)
  * exclusion — what must be healthy if this cause is the whole story
A diagnosis is "positively confirmed" only when a signature prediction was
observed. Winning purely on exclusions is weaker, and policy treats it so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import EvidenceItem, Hypothesis, PredictedObservation
from ..reasoner import DEPLOY_RECENT_MIN

# observable → its possible outcome labels
OBSERVABLES: dict[str, tuple[str, ...]] = {
    "version_cohort_errors": ("new_version_worse", "versions_equal"),
    "orders_db_health": ("dependency_degraded", "dependency_healthy"),
    "inventory_health": ("dependency_degraded", "dependency_healthy"),
}

# instrumented dependency → the observable describing it
DEPENDENCY_OBSERVABLE = {
    "orders-db": "orders_db_health",
    "inventory-service": "inventory_health",
}


@dataclass
class LiveHypothesis:
    hyp: Hypothesis
    kind: str                         # "deploy" | "dependency"
    target: str                       # release version or dependency name
    remediation: str                  # action id in REMEDIATIONS
    roles: dict[str, str] = field(default_factory=dict)  # observable → signature|exclusion

    @property
    def id(self) -> str:
        return self.hyp.id

    def prediction(self, observable: str) -> Optional[str]:
        for po in self.hyp.predicted_observations:
            if po.observable == observable:
                return po.prediction
        return None

    def signature(self) -> set[str]:
        return {o for o, r in self.roles.items() if r == "signature"}


def _hyp(hid: str, claim: str, owner: str, preds: list[tuple[str, str, str, str]]) -> tuple[Hypothesis, dict]:
    roles = {obs: role for obs, _, role, _ in preds}
    h = Hypothesis(id=hid, claim=claim, agent_id=owner, generated_by="dynamic",
                   predicted_observations=[PredictedObservation(observable=o, prediction=p, note=f"{r}: {n}")
                                           for o, p, r, n in preds])
    return h, roles


def generate_hypotheses(service: str, fired_at_ts: float, last_release: Optional[dict],
                        release_ts: Optional[float], dependencies: list[str]) -> list[LiveHypothesis]:
    out: list[LiveHypothesis] = []

    if last_release and release_ts is not None:
        mins = (fired_at_ts - release_ts) / 60.0
        if 0 <= mins <= DEPLOY_RECENT_MIN:
            v, kind = last_release["version"], last_release["kind"]
            preds = [("version_cohort_errors", "new_version_worse", "signature",
                      "errors concentrate in the new version's traffic")]
            for dep in dependencies:
                preds.append((DEPENDENCY_OBSERVABLE[dep], "dependency_healthy", "exclusion",
                              f"{dep} should be healthy if the release is the whole story"))
            h, roles = _hyp(
                "H_deploy",
                f"The {kind} release {v}, deployed {mins:.1f} min before the alert, introduced a "
                f"regression in {service} — roll it back.",
                "change_agent", preds)
            out.append(LiveHypothesis(h, "deploy", v, "rollback_deploy", roles))

    for dep in dependencies:
        obs = DEPENDENCY_OBSERVABLE[dep]
        hid = "H_" + dep.replace("-service", "").replace("-", "_")
        h, roles = _hyp(
            hid,
            f"{dep} is degraded; the dependency slowdown is cascading into {service} errors.",
            "telemetry_agent",
            [(obs, "dependency_degraded", "signature", f"{dep} latency or error rate is elevated now"),
             ("version_cohort_errors", "versions_equal", "exclusion",
              "a dependency fault hurts every version equally")])
        remediation = "failover_database" if dep == "orders-db" else "enable_inventory_fallback"
        out.append(LiveHypothesis(h, "dependency", dep, remediation, roles))
    return out


# --- probes ------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    observable: str
    description: str
    cost_ms: int               # declared expected cost — what selection trades information against
    interventional: bool = False
    dependency: Optional[str] = None


PROBES: list[ProbeSpec] = [
    ProbeSpec("P_orders_db_health", "orders_db_health",
              "orders-db client latency p95 and error rate, now vs pre-incident baseline",
              cost_ms=400, dependency="orders-db"),
    ProbeSpec("P_inventory_health", "inventory_health",
              "inventory-service client latency p95 and error rate, now vs pre-incident baseline",
              cost_ms=400, dependency="inventory-service"),
    ProbeSpec("P_version_cohorts", "version_cohort_errors",
              "route 20% of traffic to the previous version for 8s and compare error rates by version",
              cost_ms=9500, interventional=True),
]

CANARY_PCT = 20.0
CANARY_DURATION_S = 8.0


# --- remediations ------------------------------------------------------------

@dataclass(frozen=True)
class RemediationSpec:
    action: str
    description: str
    reversible: bool
    blast_radius: str          # "service" | "shared_dependency" | "platform"


REMEDIATIONS: dict[str, RemediationSpec] = {
    "rollback_deploy": RemediationSpec(
        "rollback_deploy", "roll checkout-service back to the previous version", True, "service"),
    "enable_inventory_fallback": RemediationSpec(
        "enable_inventory_fallback", "serve cached inventory instead of calling inventory-service",
        True, "service"),
    "failover_database": RemediationSpec(
        "failover_database", "fail orders-db over to its replica (drops all in-flight connections)",
        False, "shared_dependency"),
}


# --- knowledge base for the history agent -------------------------------------

RUNBOOKS = [
    {"id": "RB-112", "title": "Bad release",
     "signature": "errors concentrated in the newest version after a release; regression introduced by the release",
     "resolution": "roll back the release, then verify the error rate returns to baseline"},
    {"id": "RB-207", "title": "Dependency saturation",
     "signature": "dependency latency and connection errors cascading into checkout; dependency degraded",
     "resolution": "shed load or fail over the dependency — requires the owning team"},
]


def runbook_evidence() -> list[EvidenceItem]:
    return [EvidenceItem(source_type="runbook", source_uri=f"runbook://sre/{rb['id']}",
                         retrieved_at=None, payload=rb) for rb in RUNBOOKS]
