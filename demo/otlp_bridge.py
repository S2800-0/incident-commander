"""Corpus → OTLP bridge — proves the OTLP ingestion pipe end-to-end today.

Every bundle's evidence is re-encoded as spec-compliant OTLP JSON and POSTed
to the receiver on the running FastAPI backend. Same wire format an OTel
Collector would produce; same code path an OTel-fed production system would
exercise. The only thing that changes in production is the source of the
JSON — NiFi routes from a real Collector instead of from this script.

Usage:
    # start the backend first
    uvicorn server.app:app --port 8000

    # then push
    python -m demo.otlp_bridge                 # all bundles in corpus/
    python -m demo.otlp_bridge INC-4478         # one bundle
    python -m demo.otlp_bridge --endpoint http://localhost:8000  # override
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Repo root on path so this runs standalone (python demo/otlp_bridge.py)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ic.bundle import load_bundle, load_corpus
from ic.models import EvidenceItem
from ic.otlp import evidence_to_otlp

CORPUS = ROOT / "corpus"


def _post_json(url: str, body: dict, timeout: float = 5.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def push_bundle(bundle_path: Path, endpoint: str) -> dict:
    b = load_bundle(bundle_path)
    items: list[EvidenceItem] = b.prior_evidence()
    otlp = evidence_to_otlp(
        items,
        service_name=b.alert.get("service", "unknown-service"),
        environment=b.alert.get("environment", "production"),
    )
    resp = _post_json(f"{endpoint}/ingest/otlp/v1/metrics", otlp)
    return {"bundle": b.incident_id, "prior_items": len(items),
            "posted_metrics": sum(len(sm["metrics"])
                                    for rm in otlp["resourceMetrics"]
                                    for sm in rm["scopeMetrics"]),
            "receiver_ack": resp}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Push corpus bundles as OTLP")
    p.add_argument("incident_id", nargs="?", default=None,
                   help="specific bundle id (e.g. INC-4478); default: all")
    p.add_argument("--endpoint", default="http://localhost:8000",
                   help="FastAPI base URL (default: http://localhost:8000)")
    args = p.parse_args(argv)

    if args.incident_id:
        paths = [CORPUS / f"{args.incident_id}.json"]
    else:
        paths = sorted(CORPUS.glob("INC-*.json"))

    results = []
    for path in paths:
        try:
            r = push_bundle(path, args.endpoint)
            results.append(r)
            print(f"  [OK]  {r['bundle']:12s}  prior_items={r['prior_items']:2d}  "
                  f"metrics_posted={r['posted_metrics']:2d}  "
                  f"receiver_accepted={r['receiver_ack']['accepted']}")
        except urllib.error.URLError as e:
            print(f"  [ERR] {path.stem}: {e}")
            return 1

    print()
    print(f"pushed {len(results)} bundle(s). Confirm receiver saw them:")
    print(f"  curl {args.endpoint}/ingest/otlp/buffer?limit=5 | jq .size")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
