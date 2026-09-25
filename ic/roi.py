"""ROI computation — runs against real harness output, not a slide.

Companion to `docs/ROI.md`. That doc describes the model; this script IS the model,
computed from `harness_results.json` (the reproducible artifact from the ablation
harness). Every DATA input is read from disk; every INPUT input is either a CLI
argument or a documented default. The output distinguishes the two so a viewer
cannot mistake an assumed number for a measured one.

Usage:
    python -m ic.roi                          # defaults from docs/ROI.md
    python -m ic.roi --incidents 60 --engineer-hourly 90 --rollback-cost 800
    python -m ic.roi --json                   # emit machine-readable JSON only

What this deliberately does NOT do:
    * Does not use `time_to_conclusion_s` as a speed claim. See docs/ROI.md §3 —
      it's a fixture-data artifact, not a live-connector latency figure. We say so
      here too rather than let a future reader think we forgot.
    * Does not fabricate a "total ROI" without labeling every driver's confidence.
    * Does not silently reuse a stale harness_results.json — if it's missing or
      older than the corpus, we rerun the harness.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
HARNESS_JSON = ROOT / "harness_results.json"
CORPUS_DIR = ROOT / "corpus"


# --- INPUT defaults (mirror docs/ROI.md §4 worked example) ------------------
# These are placeholders on purpose. A real org replaces them with its own numbers.
DEFAULT_INCIDENTS_PER_MONTH = 40
DEFAULT_HARD_SLICE_FRACTION_OVERRIDE: Optional[float] = None  # None → compute from corpus
DEFAULT_ENGINEER_HOURLY_USD = 75.0
DEFAULT_ROLLBACK_COST_USD = 600.0


@dataclass
class Data:
    """Everything below is measured, not assumed."""
    corpus_size: int
    hard_slice_fraction_from_corpus: float
    accuracy_on_hard_on: float
    accuracy_on_hard_off: float
    delta_accuracy_hard: float
    fp_rollback_on: float
    fp_rollback_off: float
    delta_fp_rollback: float
    avg_human_baseline_seconds: float
    time_to_conclusion_s_note: str = (
        "excluded from ROI on purpose — fixture-data artifact, "
        "not a live-connector latency figure (see docs/ROI.md §3)"
    )


@dataclass
class Inputs:
    """Everything below is assumed. A real org substitutes its own."""
    incidents_per_month: int
    hard_slice_fraction: float           # overridable; defaults to Data.hard_slice_fraction_from_corpus
    engineer_hourly_usd: float
    rollback_cost_usd: float
    hard_slice_source: str               # "corpus" | "cli_override"


@dataclass
class Drivers:
    driver_a_monthly_usd: float
    driver_b_monthly_usd: float
    total_monthly_usd: float
    total_annual_usd: float


@dataclass
class Sensitivity:
    """±50% swing on each INPUT, holding others at their central value."""
    variable: str
    low_input: float
    central_input: float
    high_input: float
    low_total: float
    central_total: float
    high_total: float


@dataclass
class Report:
    generated_at: str
    data: Data
    inputs: Inputs
    drivers: Drivers
    sensitivity: list[Sensitivity] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


# --- data extraction --------------------------------------------------------

def _load_harness() -> dict:
    """Load harness_results.json, running the harness if the file is stale or missing.
    Stale = older than the corpus directory's most recent bundle. This keeps the ROI
    number honest when someone adds a bundle and forgets to rerun."""
    from .harness import main as run_harness

    def _corpus_mtime() -> float:
        return max((p.stat().st_mtime for p in CORPUS_DIR.glob("*.json")), default=0.0)

    if not HARNESS_JSON.exists() or HARNESS_JSON.stat().st_mtime < _corpus_mtime():
        # print to stderr so `--json` stdout stays clean
        print("(harness_results.json missing or stale — regenerating)", file=sys.stderr)
        run_harness(str(CORPUS_DIR), str(HARNESS_JSON))
    return json.loads(HARNESS_JSON.read_text())


def _extract_data(harness: dict) -> Data:
    summary = harness["summary"]
    runs = harness["runs"]

    on_runs = [r for r in runs if r["probes_enabled"]]
    unique_bundles = {r["incident_id"] for r in on_runs}
    corpus_size = len(unique_bundles)

    # Hard-slice fraction — what fraction of THIS corpus is in the ambiguous/adversarial
    # slices, the ones where the mechanism actually earns its keep. This is corpus
    # composition, not organizational reality; expose both.
    hard_slices = {"ambiguous", "adversarial_redherring"}
    hard_bundles = {r["incident_id"] for r in on_runs if r["slice"] in hard_slices}
    hard_frac = len(hard_bundles) / corpus_size if corpus_size else 0.0

    head = summary["headline_ablation"]
    fp = summary["false_positive_rollback_rate"]

    # Human baseline: mean across bundles (each bundle appears twice in runs — once ON,
    # once OFF — but has the same baseline both times). De-duplicate by incident_id.
    seen: dict[str, int] = {}
    for r in runs:
        seen.setdefault(r["incident_id"], r["human_baseline_seconds"])
    avg_baseline = statistics.mean(seen.values()) if seen else 0.0

    return Data(
        corpus_size=corpus_size,
        hard_slice_fraction_from_corpus=hard_frac,
        accuracy_on_hard_on=head["probes_on"],
        accuracy_on_hard_off=head["probes_off"],
        delta_accuracy_hard=head["delta"],
        fp_rollback_on=fp["probes_on"],
        fp_rollback_off=fp["probes_off"],
        delta_fp_rollback=fp["probes_off"] - fp["probes_on"],
        avg_human_baseline_seconds=avg_baseline,
    )


# --- ROI formula ------------------------------------------------------------

def _drivers(data: Data, inputs: Inputs) -> Drivers:
    cost_per_second = inputs.engineer_hourly_usd / 3600.0

    driver_a = (
        inputs.incidents_per_month
        * inputs.hard_slice_fraction
        * data.delta_accuracy_hard
        * data.avg_human_baseline_seconds
        * cost_per_second
    )
    driver_b = (
        inputs.incidents_per_month
        * data.delta_fp_rollback
        * inputs.rollback_cost_usd
    )
    total = driver_a + driver_b
    return Drivers(
        driver_a_monthly_usd=round(driver_a, 2),
        driver_b_monthly_usd=round(driver_b, 2),
        total_monthly_usd=round(total, 2),
        total_annual_usd=round(total * 12, 2),
    )


def _sensitivity(data: Data, inputs: Inputs) -> list[Sensitivity]:
    """±50% on each input, one at a time, others held central. Reveals which
    assumption moves the total most — useful for saying "the answer hinges on X"
    honestly rather than pretending the point estimate is precise."""
    central_total = _drivers(data, inputs).total_monthly_usd
    out: list[Sensitivity] = []
    swept = [
        ("incidents_per_month", inputs.incidents_per_month, int),
        ("hard_slice_fraction", inputs.hard_slice_fraction, float),
        ("engineer_hourly_usd", inputs.engineer_hourly_usd, float),
        ("rollback_cost_usd", inputs.rollback_cost_usd, float),
    ]
    for name, central, cast in swept:
        low_val, high_val = cast(central * 0.5), cast(central * 1.5)
        low_inputs = _replace(inputs, name, low_val)
        high_inputs = _replace(inputs, name, high_val)
        out.append(Sensitivity(
            variable=name,
            low_input=low_val, central_input=central, high_input=high_val,
            low_total=_drivers(data, low_inputs).total_monthly_usd,
            central_total=central_total,
            high_total=_drivers(data, high_inputs).total_monthly_usd,
        ))
    # sort by absolute range (high − low) — biggest lever first
    out.sort(key=lambda s: abs(s.high_total - s.low_total), reverse=True)
    return out


def _replace(inputs: Inputs, field_name: str, value: float) -> Inputs:
    return Inputs(**{**inputs.__dict__, field_name: value})


# --- rendering --------------------------------------------------------------

def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}" if abs(x) < 10 else f"${x:,.0f}"


def _render_terminal(report: Report) -> str:
    d, i, r = report.data, report.inputs, report.drivers
    lines: list[str] = []
    W = 72
    lines.append("=" * W)
    lines.append("INCIDENT COMMANDER — ROI COMPUTATION")
    lines.append("=" * W)
    lines.append(f"generated: {report.generated_at}")
    lines.append("")
    lines.append("DATA (measured — reproducible via `python -m ic.harness`):")
    lines.append(f"  corpus size                              {d.corpus_size} bundles")
    lines.append(f"  hard-slice fraction of corpus            {d.hard_slice_fraction_from_corpus:.0%}")
    lines.append(f"  accuracy on hard slices    ON / OFF      {d.accuracy_on_hard_on:.0%} / {d.accuracy_on_hard_off:.0%}")
    lines.append(f"  Δ accuracy on hard slices                {d.delta_accuracy_hard:+.0%}")
    lines.append(f"  FP-rollback rate           ON / OFF      {d.fp_rollback_on:.0%} / {d.fp_rollback_off:.0%}")
    lines.append(f"  Δ FP-rollback rate                       {d.delta_fp_rollback:+.0%}")
    lines.append(f"  avg human diagnostic baseline            {d.avg_human_baseline_seconds:.0f} sec "
                 f"(≈ {d.avg_human_baseline_seconds/60:.1f} min)")
    lines.append("")
    lines.append("INPUT (assumed — replace with your org's real numbers):")
    lines.append(f"  incidents / month                        {i.incidents_per_month}")
    lines.append(f"  hard-slice fraction                      {i.hard_slice_fraction:.0%}  "
                 f"[source: {i.hard_slice_source}]")
    lines.append(f"  engineer hourly cost                     {_fmt_usd(i.engineer_hourly_usd)} / hr")
    lines.append(f"  cost per false-positive rollback         {_fmt_usd(i.rollback_cost_usd)}")
    lines.append("")
    lines.append("DRIVERS:")
    lines.append(f"  A — avoided wrong-diagnosis cost         {_fmt_usd(r.driver_a_monthly_usd)} / month")
    lines.append(f"  B — avoided false-positive rollback cost {_fmt_usd(r.driver_b_monthly_usd)} / month")
    lines.append(f"  ─────────────────────────────────────────────────────────")
    lines.append(f"  TOTAL                                    {_fmt_usd(r.total_monthly_usd)} / month")
    lines.append(f"                                           {_fmt_usd(r.total_annual_usd)} / year "
                 f"(illustrative)")
    lines.append("")
    lines.append("SENSITIVITY (±50% on each INPUT, others central; biggest lever first):")
    for s in report.sensitivity:
        lines.append(f"  {s.variable:32s}  low → high total: "
                     f"{_fmt_usd(s.low_total)} → {_fmt_usd(s.high_total)}")
    lines.append("")
    lines.append("CAVEATS:")
    for c in report.caveats:
        lines.append(f"  • {c}")
    lines.append("=" * W)
    return "\n".join(lines)


# --- entrypoints ------------------------------------------------------------

def compute(
    incidents_per_month: int = DEFAULT_INCIDENTS_PER_MONTH,
    hard_slice_fraction: Optional[float] = DEFAULT_HARD_SLICE_FRACTION_OVERRIDE,
    engineer_hourly_usd: float = DEFAULT_ENGINEER_HOURLY_USD,
    rollback_cost_usd: float = DEFAULT_ROLLBACK_COST_USD,
) -> Report:
    harness = _load_harness()
    data = _extract_data(harness)

    if hard_slice_fraction is None:
        hard_slice_fraction = data.hard_slice_fraction_from_corpus
        hard_slice_source = "corpus"
    else:
        hard_slice_source = "cli_override"

    inputs = Inputs(
        incidents_per_month=incidents_per_month,
        hard_slice_fraction=hard_slice_fraction,
        engineer_hourly_usd=engineer_hourly_usd,
        rollback_cost_usd=rollback_cost_usd,
        hard_slice_source=hard_slice_source,
    )
    drivers = _drivers(data, inputs)
    sens = _sensitivity(data, inputs)

    caveats = [
        f"{data.corpus_size}-bundle corpus — deltas are reproducible but small-sample.",
        "No live-telemetry connector — assumes the mechanism transfers to real queries.",
        "human_baseline_seconds is author-estimated, not measured against real incident logs.",
        "This is a recurring-value model, not a full ROI — build/deployment cost is not modeled.",
        "time_to_conclusion_s from the harness is NOT used as a speed claim "
        "(fixture-data artifact; see docs/ROI.md §3).",
    ]

    return Report(
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        data=data, inputs=inputs, drivers=drivers,
        sensitivity=sens, caveats=caveats,
    )


def _to_dict(report: Report) -> dict:
    return {
        "generated_at": report.generated_at,
        "data": report.data.__dict__,
        "inputs": report.inputs.__dict__,
        "drivers": report.drivers.__dict__,
        "sensitivity": [s.__dict__ for s in report.sensitivity],
        "caveats": report.caveats,
    }


# =============================================================================
# LIVE MODE ROI — from repeated live fault-injection runs (demo/live_experiment.py)
# =============================================================================
#
# Three layers, never mixed:
#   MEASURED — counted from the live experiment file, per investigation mode
#   ASSUMED  — organisation-specific inputs (CLI), none measured by us
#   DERIVED  — MEASURED × ASSUMED, each formula printed next to its value
#
# The live experiment's scenario mix is designed (one of each failure shape per
# repetition), so any rate over "all trials" describes the test mix, not a
# production incident distribution. The model therefore only uses rates
# conditioned on scenario type, and takes the production mix as an input.

DEFAULT_LIVE_MANUAL_MINUTES = 30.0      # assumed on-call time to diagnose+mitigate a bad release by hand
DEFAULT_LIVE_ENGINEERS = 2              # assumed engineers engaged per incident
DEFAULT_LIVE_REVERSIBLE_SHARE = 0.25    # assumed share of incidents that are reversible release regressions
DEFAULT_LIVE_WRONG_ACTION_COST = DEFAULT_ROLLBACK_COST_USD


def _rate(n: float, d: float) -> Optional[float]:
    return (n / d) if d else None


def compute_live(path: str, incidents_per_month: int, engineer_hourly_usd: float,
                 manual_minutes: float, engineers: int, reversible_share: float,
                 wrong_action_cost_usd: float) -> dict:
    doc = json.loads(Path(path).read_text())
    trials = doc["trials"]
    modes = sorted({t["mode"] for t in trials})

    measured: dict = {"source": str(path), "modes": {}}
    for m in modes:
        mt = [t for t in trials if t["mode"] == m]
        recs = [t["incident"] for t in mt if t["incident"]]
        bad = [t for t in mt if t["scenario"] == "bad_deploy"]
        bad_resolved = sum(1 for t in bad if t["incident"] and t["incident"]["status"] == "RESOLVED")
        unfixable = [t for t in mt if t["scenario"] != "bad_deploy"]
        harmful = sum(1 for t in mt if t["incident"] and (
            t["score"]["forbidden_action_executed"] or t["score"]["unreverted_change_left"]))
        wrong = sum(1 for t in mt if t["incident"] and t["score"]["wrong_remediation_executed"])
        wrong_or_harmful = sum(1 for t in mt if t["incident"] and (
            t["score"]["wrong_remediation_executed"] or t["score"]["forbidden_action_executed"]
            or t["score"]["unreverted_change_left"]))
        resolved_recs = [t["incident"] for t in bad if t["incident"] and t["incident"]["status"] == "RESOLVED"]
        measured["modes"][m] = {
            "trials": len(mt),
            "detected": len(recs),
            "good_outcome_rate": _rate(sum(t["score"]["good_outcome"] for t in mt), len(mt)),
            "reversible_release_trials": len(bad),
            "autonomous_resolution_rate_on_reversible": _rate(bad_resolved, len(bad)),
            "unfixable_trials": len(unfixable),
            "escalated_without_harm_rate_on_unfixable": _rate(
                sum(1 for t in unfixable if t["incident"] and t["incident"]["status"].startswith("ESCALATED")
                    and not t["score"]["forbidden_action_executed"] and not t["score"]["unreverted_change_left"]),
                len(unfixable)),
            "harmful_outcomes": harmful,
            "harmful_outcome_rate": _rate(harmful, len(recs)),
            "wrong_remediations_executed": wrong,
            "incidents_with_wrong_or_harmful_action": wrong_or_harmful,
            "wrong_or_harmful_action_rate": _rate(wrong_or_harmful, len(recs)),
            "wrong_remediations_caught_and_reverted": sum(
                1 for t in mt if t["incident"] and t["score"]["wrong_remediation_executed"]
                and (t["incident"].get("auto_revert") or {}).get("executed")),
            "policy_denies": sum(r.get("policy_denies") or 0 for r in recs),
            "mean_detection_latency_s": _mean_or_none([t["detection_latency_s"] for t in mt]),
            "mean_time_to_verified_recovery_s": _mean_or_none(
                [r.get("time_to_verified_recovery_s") for r in resolved_recs]),
            "mean_investigation_s": _mean_or_none([r.get("investigation_duration_s") for r in recs]),
            "mean_probes_per_incident": _mean_or_none([r.get("probes_run") for r in recs]),
            "mean_wasted_probes_per_incident": _mean_or_none([r.get("probes_wasted") for r in recs]),
            "mean_canary_exposure_pct_s": _mean_or_none([r.get("canary_exposure_pct_s") for r in recs]),
            # counted only until the agent CLOSES the incident — an escalation closes
            # early while the outage continues, so this is NOT customer impact avoided
            "mean_failed_requests_until_agent_closed": _mean_or_none(
                [(r.get("customer_impact") or {}).get("failed_requests") for r in recs]),
            "chains_verified": sum(1 for r in recs if (r.get("chain") or {}).get("verified")),
        }

    assumed = {
        "incidents_per_month": incidents_per_month,
        "engineer_hourly_usd": engineer_hourly_usd,
        "manual_minutes_to_mitigate_reversible_regression": manual_minutes,
        "engineers_engaged_per_incident": engineers,
        "reversible_release_share_of_incidents": reversible_share,
        "cost_per_harmful_or_wrong_action_usd": wrong_action_cost_usd,
    }

    derived: dict = {}
    eig = measured["modes"].get("eig")
    if eig:
        auto = eig["autonomous_resolution_rate_on_reversible"] or 0.0
        automated = incidents_per_month * reversible_share * auto
        t_rec_min = (eig["mean_time_to_verified_recovery_s"] or 0.0) / 60.0
        saved_min_each = max(0.0, manual_minutes - t_rec_min)
        hours = automated * saved_min_each / 60.0 * engineers
        derived["incidents_resolved_without_a_human_per_month"] = {
            "value": round(automated, 2),
            "formula": "incidents_per_month × reversible_share × measured autonomous_resolution_rate"}
        derived["time_to_recovery_saved_per_automated_incident_min"] = {
            "value": round(saved_min_each, 2),
            "formula": "assumed manual_minutes − measured mean_time_to_verified_recovery"}
        derived["engineer_hours_avoided_per_month"] = {
            "value": round(hours, 2),
            "formula": "automated incidents × minutes saved / 60 × engineers_engaged"}
        derived["engineer_cost_avoided_per_month_usd"] = {
            "value": round(hours * engineer_hourly_usd, 2),
            "formula": "engineer_hours_avoided × engineer_hourly_usd"}

        base = measured["modes"].get("no_probes")
        if base and base["wrong_or_harmful_action_rate"] is not None and eig["wrong_or_harmful_action_rate"] is not None:
            base_bad = base["wrong_or_harmful_action_rate"]
            eig_bad = eig["wrong_or_harmful_action_rate"]
            derived["wrong_or_harmful_actions_avoided_vs_no_investigation_per_month"] = {
                "value": round(incidents_per_month * max(0.0, base_bad - eig_bad), 2),
                "formula": "incidents_per_month × (measured no_probes rate − measured eig rate) of wrong/harmful actions "
                           "— applies the TEST-MIX rate; production mix will differ"}
            derived["wrong_action_cost_avoided_per_month_usd"] = {
                "value": round(incidents_per_month * max(0.0, base_bad - eig_bad) * wrong_action_cost_usd, 2),
                "formula": "above × cost_per_harmful_or_wrong_action_usd"}

        exh = measured["modes"].get("exhaustive")
        if exh and exh["mean_probes_per_incident"] and eig["mean_probes_per_incident"] is not None:
            derived["probes_avoided_vs_exhaustive_per_incident"] = {
                "value": round(exh["mean_probes_per_incident"] - eig["mean_probes_per_incident"], 2),
                "formula": "measured exhaustive mean probes − measured eig mean probes"}
            derived["canary_traffic_exposure_avoided_vs_exhaustive_pct_s"] = {
                "value": round((exh["mean_canary_exposure_pct_s"] or 0) - (eig["mean_canary_exposure_pct_s"] or 0), 1),
                "formula": "measured exhaustive − measured eig mean (% of traffic × seconds sent to a canary)"}

    caveats = [
        "Target is a simulated service on one machine: latency, fault shapes and traffic (60 rps) are synthetic, "
        "though every request, metric, policy decision and action in the runs was real.",
        "Scenario mix is designed (equal counts per failure shape) — rates over all trials describe the test mix, "
        "not production; the model uses per-scenario-type rates and takes the production mix as an ASSUMED input.",
        "manual_minutes is assumed, not measured against human responders on these scenarios.",
        "Small n: see trials per mode. Treat rates as indicative, not as confidence-bounded estimates.",
        "Only 4 failure shapes and a template hypothesis space; causes outside it are only caught by verification.",
        "mean_failed_requests_until_agent_closed stops counting when the agent closes the incident. An escalation "
        "closes in milliseconds while the fault continues, so a mode that gives up fastest scores lowest — do not "
        "read it as customer impact avoided.",
        "The no_probes baseline executes nothing because OPA denies every naive action; its zero wrong actions are "
        "the policy's doing. Compare modes on resolution rate, not on wrong-action counts alone.",
    ]
    return {"kind": "live", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "measured": measured, "assumed": assumed, "derived": derived, "caveats": caveats}


def _mean_or_none(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def _render_live(r: dict) -> str:
    W = 78
    L = ["=" * W, "INCIDENT COMMANDER — LIVE-MODE ROI", "=" * W, f"generated: {r['generated_at']}", ""]
    L.append(f"MEASURED (live runs: {r['measured']['source']}):")
    modes = r["measured"]["modes"]
    keys = [k for k in next(iter(modes.values())).keys()] if modes else []
    L.append(f"  {'metric':52s}" + "".join(f"{m:>12s}" for m in modes))
    for k in keys:
        row = []
        for m in modes:
            v = modes[m][k]
            if isinstance(v, float) and ("rate" in k):
                row.append(f"{v:>12.0%}")
            elif v is None:
                row.append(f"{'—':>12s}")
            else:
                row.append(f"{v:>12}")
        L.append(f"  {k:52s}" + "".join(row))
    L.append("")
    L.append("ASSUMED (replace with your organisation's numbers):")
    for k, v in r["assumed"].items():
        L.append(f"  {k:52s}{v}")
    L.append("")
    L.append("DERIVED (MEASURED × ASSUMED):")
    for k, v in r["derived"].items():
        L.append(f"  {k}: {v['value']}")
        L.append(f"      = {v['formula']}")
    L.append("")
    L.append("CAVEATS:")
    for c in r["caveats"]:
        L.append(f"  • {c}")
    L.append("=" * W)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Compute ROI for Incident Commander from real harness output.",
        epilog="DATA is measured from the harness; INPUT is assumed. Replace INPUT "
               "with real org numbers before presenting the total as anything but "
               "illustrative.",
    )
    p.add_argument("--live", type=str, default=None, metavar="EXPERIMENT_JSON",
                   help="compute live-mode ROI from a demo/live_experiment.py output file "
                        "(e.g. live_runs/experiment_latest.json) instead of the replay harness")
    p.add_argument("--manual-minutes", type=float, default=DEFAULT_LIVE_MANUAL_MINUTES,
                   help="[live] assumed manual minutes to diagnose and mitigate a bad release")
    p.add_argument("--engineers", type=int, default=DEFAULT_LIVE_ENGINEERS,
                   help="[live] assumed engineers engaged per incident")
    p.add_argument("--reversible-share", type=float, default=DEFAULT_LIVE_REVERSIBLE_SHARE,
                   help="[live] assumed share of incidents that are reversible release regressions")
    p.add_argument("--incidents", type=int, default=DEFAULT_INCIDENTS_PER_MONTH,
                   metavar="N", help=f"incidents per month (default: {DEFAULT_INCIDENTS_PER_MONTH})")
    p.add_argument("--hard-slice-pct", type=float, default=None, metavar="FRAC",
                   help="fraction of incidents that are ambiguous/adversarial "
                        "(default: computed from corpus)")
    p.add_argument("--engineer-hourly", type=float, default=DEFAULT_ENGINEER_HOURLY_USD,
                   metavar="USD", help=f"fully-loaded engineer cost per hour "
                                       f"(default: ${DEFAULT_ENGINEER_HOURLY_USD:.0f})")
    p.add_argument("--rollback-cost", type=float, default=DEFAULT_ROLLBACK_COST_USD,
                   metavar="USD", help=f"cost per false-positive rollback "
                                        f"(default: ${DEFAULT_ROLLBACK_COST_USD:.0f})")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON on stdout (no terminal report)")
    p.add_argument("--out", type=str, default=None, metavar="PATH",
                   help="also write the JSON to this path")
    args = p.parse_args(argv)

    if args.live:
        live_report = compute_live(args.live, args.incidents, args.engineer_hourly, args.manual_minutes,
                                   args.engineers, args.reversible_share, args.rollback_cost)
        if args.out:
            Path(args.out).write_text(json.dumps(live_report, indent=2))
        print(json.dumps(live_report, indent=2) if args.json else _render_live(live_report))
        return 0

    report = compute(
        incidents_per_month=args.incidents,
        hard_slice_fraction=args.hard_slice_pct,
        engineer_hourly_usd=args.engineer_hourly,
        rollback_cost_usd=args.rollback_cost,
    )
    payload = _to_dict(report)

    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2))

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(_render_terminal(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
