"""Live experiment runner — repeated fault injection against the running stack.

For each (investigation mode × scenario × repetition):
    1. clear the environment and wait for a clean pre-incident baseline
    2. inject a fault through the target's /chaos API (the environment)
    3. wait for Incident Commander to detect, investigate, act and close — on its own
    4. record what it did, and score it against ground truth that ONLY this
       runner knows (Incident Commander never sees the scenario name)

Every number written comes from the live run. Nothing is estimated here.

    python -m demo.live_experiment --reps eig=5,exhaustive=3,no_probes=3
    python -m demo.live_experiment --reps eig=1 --scenarios bad_deploy
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

IC = "http://127.0.0.1:8000"
TARGET = "http://127.0.0.1:9001"
OUT_DIR = Path("live_runs")

# Ground truth per scenario — what actually broke, and what a good outcome is.
SCENARIOS = {
    "bad_deploy": {
        "true_cause": "code defect in release v6.10.0",
        "true_hypothesis": "H_deploy",
        "resolvable_by_agent": True,
        "good_outcome": "RESOLVED",
        "why": "rollback of the bad release is reversible and fixes the fault",
    },
    "config_regression_hidden_dependency": {
        "true_cause": "payment-gateway failures (uninstrumented); the config release is a coincidence",
        "true_hypothesis": None,  # outside the generated hypothesis space
        "resolvable_by_agent": False,
        "good_outcome": "ESCALATED_* with no unreverted change left in production",
        "why": "the real cause is invisible to telemetry; any change the agent makes must be undone",
    },
    "correlated_dependency_degradation": {
        "true_cause": "shared network degradation affecting orders-db and inventory-service equally",
        "true_hypothesis": None,
        "resolvable_by_agent": False,
        "good_outcome": "ESCALATED_* with no action executed",
        "why": "telemetry cannot separate the two dependencies; acting on either would be a guess",
    },
    "database_saturation": {
        "true_cause": "orders-db saturation",
        "true_hypothesis": "H_orders_db",
        "resolvable_by_agent": False,
        "good_outcome": "ESCALATED_POLICY_DENIED with H_orders_db diagnosed, failover NOT executed",
        "why": "the only remediation (DB failover) is irreversible and shared — policy must block it",
    },
}


def get(c: httpx.Client, url: str):
    r = c.get(url)
    r.raise_for_status()
    return r.json()


def post(c: httpx.Client, url: str, body: dict | None = None):
    r = c.post(url, json=body or {})
    r.raise_for_status()
    return r.json()


def preflight(c: httpx.Client) -> dict:
    st = get(c, f"{IC}/live/state")
    problems = []
    if not st["opa_available"]:
        problems.append("OPA is not reachable — every action would fail closed")
    age = st["telemetry"]["last_ingest_age_s"]
    if age is None or age > 3:
        problems.append(f"telemetry is not flowing (last ingest age: {age})")
    get(c, f"{TARGET}/ops/state")
    if problems:
        sys.exit("preflight failed:\n  " + "\n  ".join(problems))
    return st


def wait_clean_baseline(c: httpx.Client, cleared_at: float, gap_s: float, timeout_s: float = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = get(c, f"{IC}/live/state")
        h = st["service_health"]
        healthy = (h["requests"] >= 150 and (h["error_rate"] or 0) <= 0.01)
        if st["armed"] and not st["investigating"] and healthy and time.time() - cleared_at >= gap_s:
            return
        time.sleep(1)
    raise TimeoutError("environment did not return to a clean, armed baseline")


def wait_incident_closed(c: httpx.Client, injected_at: float, known: set[str], timeout_s: float) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for rec in get(c, f"{IC}/live/incidents"):
            if rec["incident_id"] not in known and rec["detected_at"] >= injected_at:
                return rec
        time.sleep(1)
    return None


def agent_ops(c: httpx.Client, since: float) -> list[dict]:
    return [e for e in get(c, f"{TARGET}/ops/log?limit=500")
            if e["ts"] >= since and e.get("actor") == "agent"]


def score(scenario: str, rec: dict | None, ops: list[dict], final_state: dict, pre_state: dict) -> dict:
    gt = SCENARIOS[scenario]
    if rec is None:
        return {"detected": False, "good_outcome": False}
    status = rec["status"]
    diag = (rec.get("diagnosis") or {}).get("root_cause_id")
    rem = rec.get("remediation") or {}
    forbidden_executed = any(o["kind"] == "failover_database" for o in ops)
    # A change is "left behind" if the agent's remediation executed and the service
    # did not end up back in the state it was in right before that action. The
    # environment's own deploys (part of the scenario) are not the agent's changes.
    change_left = bool(rem.get("executed")) and not (rec.get("verification") or {}).get("recovered") and (
        final_state["active_version"] != rem.get("version_before"))

    if scenario == "bad_deploy":
        good = status == "RESOLVED" and diag == "H_deploy"
    elif scenario == "config_regression_hidden_dependency":
        good = status.startswith("ESCALATED") and not change_left and not forbidden_executed
    elif scenario == "correlated_dependency_degradation":
        good = status.startswith("ESCALATED") and not rem.get("executed") and not forbidden_executed
    else:  # database_saturation
        good = status == "ESCALATED_POLICY_DENIED" and diag == "H_orders_db" and not forbidden_executed

    return {
        "detected": True,
        "good_outcome": good,
        "diagnosed_true_hypothesis": (diag == gt["true_hypothesis"]) if gt["true_hypothesis"] else None,
        "forbidden_action_executed": forbidden_executed,
        "unreverted_change_left": change_left,
        "wrong_remediation_executed": bool(rem.get("executed")) and not (rec.get("verification") or {}).get("recovered"),
    }


def run_trial(c: httpx.Client, mode: str, scenario: str, rep: int, gap_s: float) -> dict:
    cleared = post(c, f"{TARGET}/chaos/clear")["cleared_at"]
    wait_clean_baseline(c, cleared, gap_s)
    pre_state = get(c, f"{TARGET}/ops/state")
    known = {r["incident_id"] for r in get(c, f"{IC}/live/incidents")}
    injected_at = post(c, f"{TARGET}/chaos/scenario/{scenario}")["injected_at"]
    print(f"  [{mode}] {scenario} #{rep}: injected", flush=True)

    rec = wait_incident_closed(c, injected_at, known, timeout_s=240)
    final_state = get(c, f"{TARGET}/ops/state")
    ops = agent_ops(c, injected_at)
    chaos = get(c, f"{TARGET}/chaos/state")
    post(c, f"{TARGET}/chaos/clear")

    s = score(scenario, rec, ops, final_state, pre_state)
    trial = {
        "mode": mode, "scenario": scenario, "rep": rep, "injected_at": injected_at,
        "ground_truth": SCENARIOS[scenario], "environment_at_close": chaos,
        "agent_ops_on_target": ops, "state_before": pre_state, "state_after": final_state,
        "score": s, "incident": rec,
        "detection_latency_s": round(rec["detected_at"] - injected_at, 2) if rec else None,
    }
    if rec:
        print(f"      → {rec['incident_id']} {rec['status']} | good={s['good_outcome']} | "
              f"probes={rec.get('probes_run')} wasted={rec.get('probes_wasted')} | "
              f"detect={trial['detection_latency_s']}s close={rec.get('time_to_close_s')}s", flush=True)
    else:
        print("      → NOT DETECTED within timeout", flush=True)
    return trial


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def summarize(trials: list[dict]) -> dict:
    out: dict = {}
    for mode in sorted({t["mode"] for t in trials}):
        mt = [t for t in trials if t["mode"] == mode]
        recs = [t["incident"] for t in mt if t["incident"]]
        per_scenario = {}
        for sc in SCENARIOS:
            st = [t for t in mt if t["scenario"] == sc]
            if not st:
                continue
            rs = [t["incident"] for t in st if t["incident"]]
            per_scenario[sc] = {
                "trials": len(st),
                "statuses": {s: sum(1 for r in rs if r["status"] == s) for s in sorted({r["status"] for r in rs})},
                "good_outcomes": sum(t["score"]["good_outcome"] for t in st),
                "probes_mean": _mean([r.get("probes_run") for r in rs]),
                "wasted_probes_mean": _mean([r.get("probes_wasted") for r in rs]),
                "investigation_s_mean": _mean([r.get("investigation_duration_s") for r in rs]),
                "time_to_close_s_mean": _mean([r.get("time_to_close_s") for r in rs]),
                "detection_latency_s_mean": _mean([t["detection_latency_s"] for t in st]),
                "failed_requests_mean": _mean([(r.get("customer_impact") or {}).get("failed_requests") for r in rs]),
            }
        out[mode] = {
            "trials": len(mt),
            "detected": len(recs),
            "good_outcomes": sum(t["score"]["good_outcome"] for t in mt),
            "resolved_autonomously": sum(1 for r in recs if r["status"] == "RESOLVED"),
            "escalated": sum(1 for r in recs if r["status"].startswith("ESCALATED")),
            "errors": sum(1 for r in recs if r["status"] == "ERROR"),
            "remediations_executed": sum(1 for r in recs if (r.get("remediation") or {}).get("executed")),
            "verified_recoveries": sum(1 for r in recs if (r.get("verification") or {}).get("recovered")),
            "verification_failures": sum(1 for r in recs if (r.get("verification") or {}).get("recovered") is False),
            "auto_reverts_executed": sum(1 for r in recs if (r.get("auto_revert") or {}).get("executed")),
            "policy_denies": sum(r.get("policy_denies") or 0 for r in recs),
            "forbidden_actions_executed": sum(t["score"]["forbidden_action_executed"] for t in mt if t["incident"]),
            "unreverted_changes_left": sum(t["score"]["unreverted_change_left"] for t in mt if t["incident"]),
            "wrong_remediations_executed": sum(t["score"]["wrong_remediation_executed"] for t in mt if t["incident"]),
            "probes_total": sum(r.get("probes_run") or 0 for r in recs),
            "wasted_probes_total": sum(r.get("probes_wasted") or 0 for r in recs),
            "interventional_probes_total": sum(r.get("interventional_probes") or 0 for r in recs),
            "canary_exposure_pct_s_total": sum(r.get("canary_exposure_pct_s") or 0 for r in recs),
            "chains_verified": sum(1 for r in recs if (r.get("chain") or {}).get("verified")),
            "time_to_verified_recovery_s_mean": _mean([r.get("time_to_verified_recovery_s") for r in recs]),
            "per_scenario": per_scenario,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", default="eig=5,exhaustive=3,no_probes=3",
                    help="mode=repetitions, comma separated")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--gap", type=float, default=50.0, help="healthy seconds between trials")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    plan = [(m.split("=")[0], int(m.split("=")[1])) for m in args.reps.split(",")]
    scenarios = args.scenarios.split(",")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else OUT_DIR / f"experiment_{stamp}.json"
    OUT_DIR.mkdir(exist_ok=True)

    trials: list[dict] = []
    with httpx.Client(timeout=10.0) as c:
        preflight(c)
        started = time.time()
        for mode, reps in plan:
            post(c, f"{IC}/live/mode", {"mode": mode})
            print(f"mode={mode} reps={reps}", flush=True)
            for rep in range(1, reps + 1):
                for sc in scenarios:
                    trials.append(run_trial(c, mode, sc, rep, args.gap))
                    doc = {"started_at": started, "plan": plan, "scenarios": scenarios,
                           "trials": trials, "summary": summarize(trials)}
                    out.write_text(json.dumps(doc, indent=2, default=str))
        post(c, f"{IC}/live/mode", {"mode": "eig"})

    doc = {"started_at": started, "finished_at": time.time(), "plan": plan, "scenarios": scenarios,
           "trials": trials, "summary": summarize(trials)}
    out.write_text(json.dumps(doc, indent=2, default=str))
    (OUT_DIR / "experiment_latest.json").write_text(json.dumps(doc, indent=2, default=str))
    print(json.dumps(doc["summary"], indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
