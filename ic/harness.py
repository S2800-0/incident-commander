"""Incident Replay Harness — Step 4, the proof.

Runs every bundle twice (probes ON / OFF) and reports the ablation: top-1 accuracy on
the ambiguous + adversarial slices, the false-positive rollback rate, Brier score, and
reliability bins. Writes harness_results.json for the console.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .bundle import load_corpus
from .orchestrator import run_investigation

HEADLINE_SLICES = {"ambiguous", "adversarial_redherring"}


@dataclass
class Run:
    incident_id: str
    slice: str
    probes_enabled: bool
    winner: str
    truth: str
    top1_correct: bool
    final_posterior: float
    rollback_recommended: bool
    rollback_correct: bool
    rollback_match: bool
    false_positive_rollback: bool
    probes_run: list[str]
    time_to_conclusion_s: float
    human_baseline_seconds: int


def run_all(corpus_dir: str = "corpus") -> list[Run]:
    runs: list[Run] = []
    for b in load_corpus(corpus_dir):
        truth = b.ground_truth["root_cause_id"]
        rb_correct = bool(b.ground_truth.get("rollback_correct", False))
        for enabled in (True, False):
            # use_llm=False AND policy_enabled=False AND dynamic_hypotheses=False
            # always: the harness is the graded artifact and must be reproducible
            # without a running Docker stack or a live LLM. Live-demo runs turn
            # each of these on individually via env flags or explicit params.
            res = run_investigation(b, probes_enabled=enabled,
                                     use_llm=False, policy_enabled=False,
                                     dynamic_hypotheses=False)
            v = res.verdict
            correct = v.root_cause_id == truth
            runs.append(Run(
                incident_id=b.incident_id, slice=b.slice, probes_enabled=enabled,
                winner=v.root_cause_id, truth=truth, top1_correct=correct,
                final_posterior=v.posterior,
                rollback_recommended=v.rollback_recommended,
                rollback_correct=rb_correct,
                rollback_match=v.rollback_recommended == rb_correct,
                false_positive_rollback=v.rollback_recommended and not rb_correct,
                probes_run=res.probes_run,
                time_to_conclusion_s=res.time_to_conclusion_s,
                human_baseline_seconds=b.human_baseline_seconds,
            ))
    return runs


def _acc(runs: list[Run]) -> float:
    return sum(r.top1_correct for r in runs) / len(runs) if runs else 0.0


def _brier(runs: list[Run]) -> float:
    # (confidence in the reported top hypothesis - 1{it was correct})^2
    return sum((r.final_posterior - (1.0 if r.top1_correct else 0.0)) ** 2
               for r in runs) / len(runs) if runs else 0.0


def reliability_bins(runs: list[Run], nbins: int = 5) -> list[dict]:
    bins = []
    for i in range(nbins):
        lo, hi = i / nbins, (i + 1) / nbins
        sel = [r for r in runs if (lo <= r.final_posterior < hi) or (hi == 1.0 and r.final_posterior == 1.0)]
        if sel:
            bins.append({"bin": f"{lo:.1f}-{hi:.1f}", "mid": (lo + hi) / 2,
                         "n": len(sel), "predicted": sum(r.final_posterior for r in sel) / len(sel),
                         "empirical": _acc(sel)})
    return bins


def summarize(runs: list[Run]) -> dict:
    on = [r for r in runs if r.probes_enabled]
    off = [r for r in runs if not r.probes_enabled]
    head_on = [r for r in on if r.slice in HEADLINE_SLICES]
    head_off = [r for r in off if r.slice in HEADLINE_SLICES]
    slices = sorted({r.slice for r in runs})

    def fp_rate(rs): return sum(r.false_positive_rollback for r in rs) / len(rs) if rs else 0.0

    return {
        "top1_accuracy": {"probes_on": _acc(on), "probes_off": _acc(off)},
        "headline_ablation": {
            "slices": sorted(HEADLINE_SLICES),
            "probes_on": _acc(head_on), "probes_off": _acc(head_off),
            "delta": _acc(head_on) - _acc(head_off),
        },
        "per_slice": {
            s: {"probes_on": _acc([r for r in on if r.slice == s]),
                "probes_off": _acc([r for r in off if r.slice == s])}
            for s in slices
        },
        "false_positive_rollback_rate": {
            "probes_on": fp_rate(on), "probes_off": fp_rate(off),
            "adversarial_on": fp_rate([r for r in on if r.slice == "adversarial_redherring"]),
            "adversarial_off": fp_rate([r for r in off if r.slice == "adversarial_redherring"]),
        },
        "brier": {"probes_on": _brier(on), "probes_off": _brier(off), "all": _brier(runs)},
        "reliability_bins": reliability_bins(runs),
    }


def main(corpus_dir: str = "corpus", out: str = "harness_results.json") -> dict:
    runs = run_all(corpus_dir)
    summary = summarize(runs)
    doc = {"runs": [asdict(r) for r in runs], "summary": summary}
    Path(out).write_text(json.dumps(doc, indent=2))

    s = summary
    print("=" * 68)
    print("INCIDENT COMMANDER — ABLATION HARNESS")
    print("=" * 68)
    print(f"\nTop-1 accuracy (all slices):  ON {s['top1_accuracy']['probes_on']:.0%}"
          f"   OFF {s['top1_accuracy']['probes_off']:.0%}")
    h = s["headline_ablation"]
    print(f"\n★ HEADLINE — accuracy on {h['slices']}:")
    print(f"    probes ON : {h['probes_on']:.0%}")
    print(f"    probes OFF: {h['probes_off']:.0%}")
    print(f"    Δ (the whole thesis): {h['delta']:+.0%}")
    print("\nPer-slice accuracy (ON / OFF):")
    for sl, v in s["per_slice"].items():
        print(f"    {sl:24s}  {v['probes_on']:.0%} / {v['probes_off']:.0%}")
    fp = s["false_positive_rollback_rate"]
    print(f"\nFalse-positive rollback rate: ON {fp['probes_on']:.0%}   OFF {fp['probes_off']:.0%}")
    print(f"    on adversarial_redherring:  ON {fp['adversarial_on']:.0%}   OFF {fp['adversarial_off']:.0%}")
    print(f"\nBrier score (lower=better):   ON {s['brier']['probes_on']:.3f}"
          f"   OFF {s['brier']['probes_off']:.3f}")
    print(f"\nWrote {out}  ({len(runs)} runs)")
    print(f"\nNOTE: {len(runs) // 2} bundles — numbers are illustrative, not "
          "publication-grade. The headline is the sign and size of the delta.")
    return doc


if __name__ == "__main__":
    main()
