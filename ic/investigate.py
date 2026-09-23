"""CLI: run an investigation and seal a postmortem.

    python -m ic.investigate INC-4471 [--no-probes] [-o postmortem.json]
"""
from __future__ import annotations

import argparse

from . import enable_utf8_stdout
from .bundle import load_bundle
from .evidence_chain import write_postmortem
from .orchestrator import run_investigation


def main() -> None:
    enable_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("incident_id")
    ap.add_argument("--no-probes", action="store_true", help="ablation-off arm")
    ap.add_argument("-o", "--out", default="postmortem.json")
    ap.add_argument("--corpus", default="corpus")
    args = ap.parse_args()

    b = load_bundle(f"{args.corpus}/{args.incident_id}.json")
    res = run_investigation(b, probes_enabled=not args.no_probes,
                            emit=lambda e: print(f"  · {e['type']}: "
                                                 f"{ {k:v for k,v in e.items() if k not in ('type','incident_id','probes_enabled')} }"))
    v = res.verdict
    print(f"\nVERDICT {v.incident_id}: root_cause={v.root_cause_id} "
          f"posterior={v.posterior} rollback_recommended={v.rollback_recommended}")
    print(f"  probes run: {res.probes_run}")
    write_postmortem(v, res.cited_items, args.out, sealed=res.sealed)
    print(f"  merkle_root: {v.merkle_root}")
    print(f"  sealed → {args.out}")
    print(f"\nVerify with:  python verify.py {args.out}")


if __name__ == "__main__":
    main()
