# Incident Commander — Feature Walkthrough

**What this document is.** A visual, click-through guide to every capability
that shipped between Sep 11 and Sep 22. Each feature has: (1) what it does,
(2) the browser screenshot showing it live, (3) where the code lives, and
(4) how to run it locally. If a judge asks "what did the final-phase pivot
ship?", this doc walks them through it in order.

**Where to find companion docs:**
  * [`docs/ROADMAP.md`](../docs/ROADMAP.md) — the nine-step production ladder.
  * [`docs/SRS.md`](../docs/SRS.md) — the formal requirements traceability matrix.
  * [`docs/RAI.md`](../docs/RAI.md) — Responsible-AI governance and the autonomy
    ladder (L0–L4).
  * [`docs/ROI.md`](../docs/ROI.md) — the business case, formula, and refreshed
    numbers from the 12-bundle corpus.
  * `demo/DEMO_SCRIPT.md` and `demo/QA_PREP.md` — LOCAL ONLY (gitignored per
    prior instruction). Kept on this machine as rehearsal aids.

---

## Prerequisites — bring the stack up

```bash
# 1. OPA policy engine (Docker sidecar)
cd "/Users/shahy/Desktop/Incident commander"
docker compose up -d opa
docker ps --filter name=ic-opa    # → Up (healthy)

# 2. Backend
uvicorn server.app:app --port 8000 --log-level warning

# 3. Console
cd console && npm install && npm run dev
# → open http://localhost:5173
```

Optional — for LLM-driven dynamic hypothesis generation:
```bash
export IC_USE_LLM=1 IC_DYNAMIC_HYPOTHESES=1   # requires .env with a key
```

---

## The console at a glance

The six panels that reflect shipped work (top-to-bottom, left-to-right on the
grid):

| Panel | What it shows | Feature | Screenshot section |
|---|---|---|---|
| **Customer Impact (CIS)** | Score, tier, affected users, SLA, revenue path | §5 CIS | [§5](#5-customer-impact-scoring-cis) |
| **Policy Engine (OPA · Rego)** | ALLOW/DENY decisions with structured reasons | §2 Policy | [§2](#2-real-policy-engine-opa--rego) |
| **Agent Lanes** | Change / Telemetry / History reasoning | Baseline mechanism | any screenshot |
| **Probe Selection** | Which discriminator was picked and why | Baseline mechanism | any screenshot |
| **Hypothesis Board** | Live competing hypotheses + winner + eliminated | Baseline mechanism | any screenshot |
| **Verification Loop (recovery check)** | RECOVERED / NOT RECOVERED + baseline/post/band | §3 Verification | [§3](#3-verification-loop--auto-revert) |
| **Evidence Chain** | Every cited leaf, Merkle root, Ed25519 signature | Baseline mechanism | any screenshot |
| **VoI · Value of the Next Action** | EIG/cost/risk per candidate action | Baseline mechanism | any screenshot |

---

## Feature 1 — Full end-to-end on INC-4478 (the flagship case)

**What this shows.** The complete pivot working on one incident: real OPA
policy decisions, real verification loop, real CIS routing, all sealed into
the evidence chain. This is the single screenshot that most fully answers
"what did you build?"

**Screenshot.** [`console_live/01_INC4478_ambiguous_intervention_success.png`](screenshots/console_live/01_INC4478_ambiguous_intervention_success.png)

**Read it top to bottom.**
  * **Customer Impact = 100 · CRITICAL** — P0, 12,000 affected users, SLA
    breach yes, revenue path yes. Impact source: *checkout POST /checkout —
    direct revenue path*.
  * **Policy Engine — ALLOW × 2** — `intervention` (Route 5% of traffic to
    prior version, canary, bounded 60s, auto-revert), then `rollback`.
  * **Agent Lanes** — Change/Telemetry/History all reasoned; each cites
    resolvable evidence.
  * **Hypothesis Board** — H2 winner @ 94% (deploy is guilty), H1 eliminated
    (`probe showed traffic_cohort_split='cohorts_diverge', but this
    hypothesis predicted 'cohorts_uniform'`).
  * **Verification Loop — RECOVERED** — `http.server.response.5xx.rate`,
    baseline 2.1, post 2.4, band ≤ 5. Post value inside band.
  * **Evidence Chain** — 7 leaves sealed, Merkle root + Ed25519 signature
    shown.

**Run it.**
```bash
open "http://localhost:5173/?auto=INC-4478&speed=20"
```

---

## Feature 2 — Real policy engine (OPA · Rego)

**What shipped.** The old "Approve" button is gone. Every state-changing
action goes through OPA policy evaluation against structured inputs
(posterior, evidence citations, safety envelope, upstream health). Rego
policies live in versioned `.rego` files that a compliance reviewer can
audit independently of Python code. Fail-closed: if OPA is unreachable, the
action is denied with reason `policy_engine_unavailable`.

**Screenshot.**
[`console_live/01_INC4478_ambiguous_intervention_success.png`](screenshots/console_live/01_INC4478_ambiguous_intervention_success.png)
— the green ALLOW × 2 panel is the shipped behavior.

Terminal-side backup:
[`opa/03_scenarios.png`](screenshots/opa/03_scenarios.png) — five real
decisions with allow/deny reasons.

**Code.**
  * [`policy/incident/action.rego`](../policy/incident/action.rego) — the
    three action classes (rollback, canary, intervention) with real
    preconditions.
  * [`ic/policy.py`](../ic/policy.py) — HTTP client, fail-closed on error.
  * [`ic/orchestrator.py`](../ic/orchestrator.py) — wires the call at the
    gate points.
  * [`docker-compose.yml`](../docker-compose.yml) — OPA sidecar service.

**Run just the policy check.**
```bash
python -m ic.policy   # single sample decision
```

---

## Feature 3 — Verification loop + auto-revert

**What shipped.** After every intervention runs, the system re-checks the
customer signal (from the bundle's `verification_result` block or, when
absent, from ground-truth alignment). `recovered=True` keeps provenance
`interventional` and marks `verified_recovery=True` on the verdict.
`recovered=False` fires `auto_revert_triggered`, records the safety envelope
being enforced, and demotes provenance to `observational`.

**Success case — screenshot.**
[`console_live/01_INC4478_ambiguous_intervention_success.png`](screenshots/console_live/01_INC4478_ambiguous_intervention_success.png)
— **RECOVERED** panel, baseline 2.1 · post 2.4 · band ≤ 5.

**Failure case — screenshot.**
[`console_live/04_INC4481_auto_revert.png`](screenshots/console_live/04_INC4481_auto_revert.png)
— **NOT RECOVERED** in red, `post_intervention_value=54.2 is outside
recovery band`, followed by **Auto-revert triggered** with envelope shown
(5% traffic · 60s · auto-revert true). Verdict at the bottom reads
`ROLLBACK NOT RECOMMENDED` + `Provenance: observational` — demoted because
the intervention did NOT resolve the incident.

**Code.**
  * [`ic/verification.py`](../ic/verification.py) — recovery-band check,
    both explicit-block and synthesized-fallback paths.
  * `evidence.verification_result` block on bundle JSON — see
    `corpus/INC-4478.json` for the schema.
  * [`ic/orchestrator.py`](../ic/orchestrator.py) provenance-demotion logic.

**Run it.**
```bash
open "http://localhost:5173/?auto=INC-4481&speed=20"     # failure path
open "http://localhost:5173/?auto=INC-4482&speed=20"     # autonomous success
```

---

## Feature 4 — Autonomous success on a distinct service

**What this proves.** The same closed loop (policy → intervention →
verification → chain sealed) runs on a different service, signal type, and
payload shape. It's not tied to the checkout-service story.

**Screenshot.**
[`console_live/05_INC4482_autonomous_success_notif.png`](screenshots/console_live/05_INC4482_autonomous_success_notif.png)
— notifications-service publish-latency. Intervention runs, canary cohort
recovers to 340ms (band ≤ 900), `verified_recovery=True`, provenance stays
`interventional`.

---

## Feature 5 — Customer-Impact Scoring (CIS)

**What shipped.** Every bundle can carry a `customer_impact` block. CIS
routes urgency (P0 revenue path → critical; P3 internal → low) but is
**structurally prevented** from touching the diagnostic path.

**High-impact case — screenshot.**
[`console_live/02_INC4479_p0_revenue_cis_critical.png`](screenshots/console_live/02_INC4479_p0_revenue_cis_critical.png)
— **CIS 100 CRITICAL**, P0, 25,000 users, SLA breach yes, revenue-tagged.

**Low-impact case — screenshot.**
[`console_live/03_INC4480_p3_internal_cis_low.png`](screenshots/console_live/03_INC4480_p3_internal_cis_low.png)
— **CIS 5 LOW**, P3, 25 internal users, no SLA, not revenue. Same
diagnostic verdict shape as INC-4479 (both roll back a deploy at 94%
posterior). CIS routes them differently; diagnosis stays identical.

**Guardrail proof.**
[`cis/04_guardrail_static.png`](screenshots/cis/04_guardrail_static.png)
— static AST audit shows zero imports from `ic.cis` across 10
diagnostic-path files. Enforced at build time, not just by comment.

**Code.**
  * [`ic/cis.py`](../ic/cis.py) — scoring formula, tier weights, urgency
    labels, evidence-item builder.
  * `customer_impact` block on bundle JSON — see `corpus/INC-4478.json`,
    `INC-4479.json`, `INC-4480.json`, `INC-4481.json`, `INC-4482.json`,
    `INC-4473.json`.
  * [`ic/orchestrator.py`](../ic/orchestrator.py) computes CIS AFTER the
    verdict is frozen — temporal separation makes contamination impossible.

---

## Feature 6 — OpenTelemetry-shaped ingestion (Apache NiFi optional)

**What shipped.** Spec-compliant OTLP JSON ingestion at `POST
/ingest/otlp/v1/{metrics,logs}`. Corpus bridge (`demo/otlp_bridge.py`)
proves the pipe today; a NiFi flow (`nifi/README.md`) shows the production
routing shape.

**Screenshots.**
  * [`otlp/01_otlp_payload.png`](screenshots/otlp/01_otlp_payload.png) — what OTLP
    JSON looks like on the wire.
  * [`otlp/02_metrics_ingest.png`](screenshots/otlp/02_metrics_ingest.png) — receiver
    accepts histogram + gauge.
  * [`otlp/04_normalized_evidence.png`](screenshots/otlp/04_normalized_evidence.png) —
    ingested payload normalized to typed `EvidenceItem`.
  * [`otlp/05_corpus_bridge.png`](screenshots/otlp/05_corpus_bridge.png) — all 8
    corpus bundles pushed end-to-end.

**Code.**
  * [`ic/otlp.py`](../ic/otlp.py) — parser and adapter.
  * [`server/app.py`](../server/app.py) — receiver endpoints + audit buffer.
  * [`demo/otlp_bridge.py`](otlp_bridge.py) — corpus → OTLP pipe.
  * [`nifi/README.md`](../nifi/README.md) — flow topology.

**Run it.**
```bash
python -m demo.otlp_bridge INC-4478                          # push one
python -m demo.otlp_bridge                                    # push all 12
curl localhost:8000/ingest/otlp/buffer | python -m json.tool  # peek what landed
```

---

## Feature 7 — Dynamic LLM hypothesis generation

**What shipped.** With `IC_USE_LLM=1 IC_DYNAMIC_HYPOTHESES=1` set, the LLM
generates 2-4 competing hypotheses live from raw incident context. Each
carries claim + supporting/contradicting evidence + predictions +
discriminating signals + confidence. Anti-hallucination filter drops any
evidence ref that doesn't resolve to real prior evidence.

**Screenshots.**
  * [`dynamic/02_generated.png`](screenshots/dynamic/02_generated.png) — three
    competing hypotheses generated live from raw context.
  * [`dynamic/03_anti_hallucination.png`](screenshots/dynamic/03_anti_hallucination.png) —
    invented refs dropped before the reasoner sees them.
  * [`dynamic/05_fallback.png`](screenshots/dynamic/05_fallback.png) — LLM
    unavailable → transparent fallback to seeded.
  * [`dynamic/06_harness_preserved.png`](screenshots/dynamic/06_harness_preserved.png) —
    graded ablation stays bit-identical regardless of env flags.

**Code.**
  * [`ic/dynamic_triage.py`](../ic/dynamic_triage.py) — strict pydantic schema
    the LLM must match; anti-hallucination filter.
  * [`ic/orchestrator.py`](../ic/orchestrator.py) — two-gate opt-in
    (`IC_USE_LLM` + `IC_DYNAMIC_HYPOTHESES`), transparent fallback.

**Run it.**
```bash
IC_USE_LLM=1 IC_DYNAMIC_HYPOTHESES=1 python -m ic.investigate INC-4478
```

---

## Feature 8 — Corpus expanded 8 → 12 bundles

**What shipped.** Four new bundles, each earning its slot by exercising a
specific shipped capability under real harness conditions.

| Bundle | Slice | Tier | What it demonstrates |
|---|---|---|---|
| **INC-4479** | deployment_induced | P0 | High-CIS revenue-path outage (payment-service, 25k users, SLA breach) |
| **INC-4480** | deployment_induced | P3 | Low-CIS internal contrast (same mechanism, opposite routing) |
| **INC-4481** | resource_exhaustion | P1 | Auto-revert path on a real bundle |
| **INC-4482** | ambiguous | P1 | Autonomous success on a new service |

**Screenshots (each one running live in the console):**
  * [`console_live/02_INC4479_p0_revenue_cis_critical.png`](screenshots/console_live/02_INC4479_p0_revenue_cis_critical.png)
  * [`console_live/03_INC4480_p3_internal_cis_low.png`](screenshots/console_live/03_INC4480_p3_internal_cis_low.png)
  * [`console_live/04_INC4481_auto_revert.png`](screenshots/console_live/04_INC4481_auto_revert.png)
  * [`console_live/05_INC4482_autonomous_success_notif.png`](screenshots/console_live/05_INC4482_autonomous_success_notif.png)

Additional context:
[`corpus/02_rationale.png`](screenshots/corpus/02_rationale.png) — the design
rationale for each new bundle.

**Code.** [`corpus/INC-4479.json`](../corpus/INC-4479.json) through
[`corpus/INC-4482.json`](../corpus/INC-4482.json).

---

## Feature 9 — Ablation harness on the wider corpus

**What shipped.** Same reproducible harness, now measuring 12 bundles. Every
new bundle passes; the headline delta is preserved.

**Screenshot.**
[`corpus/03_harness_all12.png`](screenshots/corpus/03_harness_all12.png)
— `python -m ic.harness` output.

**Key numbers on 12 bundles:**
  * Top-1 accuracy — ON 100%, OFF 25%
  * Headline delta (ambiguous + adversarial red-herring) — **+100%**
  * False-positive rollback rate — ON 0%, OFF 33%
  * Brier score — ON 0.004, OFF 0.287

**Run it.**
```bash
python -m ic.harness
```

---

## Feature 10 — Real ROI computation

**What shipped.** `python -m ic.roi` reads the harness output and computes
both drivers from real DATA. Assumption inputs are labeled clearly and can
be overridden from the CLI.

**Screenshot.**
[`corpus/06_roi_wider.png`](screenshots/corpus/06_roi_wider.png) — the
current output on the 12-bundle corpus.

**Illustrative total** (with default INPUT placeholders): $9,017 / month ·
$108,204 / year. **The model is the deliverable, not the specific figure.**

**Code.** [`ic/roi.py`](../ic/roi.py) + [`docs/ROI.md`](../docs/ROI.md) for
the honest DATA vs INPUT breakdown.

**Run it.**
```bash
python -m ic.roi                                        # defaults
python -m ic.roi --incidents 60 --rollback-cost 1200    # override INPUTs
```

---

## Feature 11 — The "probes OFF" control arm still works

**What this shows.** Even with policy on, running with `probes` unchecked
puts the mechanism in the ablation-OFF state — it can *see* the ambiguity
but can't resolve it, and (correctly) reports so. This is what the ablation
numbers are actually measuring.

**Screenshot.**
[`console_live/07_INC4478_probes_off_policy_on.png`](screenshots/console_live/07_INC4478_probes_off_policy_on.png)
— same incident, probes off, policy on. No probe fires, no intervention,
no verification. Verdict falls to whichever hypothesis prior evidence
leans toward — with the ambiguity clearly banner'd.

---

## Feature 12 — Legacy mode (no policy) still works

**What this shows.** Uncheck `policy` and the console reverts to the
pre-pivot flow: `gate_pending` events fire, an Approve button appears for
interventions, no policy or verification panels populate. Kept as a
comparison view.

**Screenshot.**
[`console_live/06_INC4471_baseline_no_policy.png`](screenshots/console_live/06_INC4471_baseline_no_policy.png)
— INC-4471 (the original ambiguous case) run with `policy=0`. The pivot
panels stay in idle state.

---

## Where to look next

  * **For a specific requirement** — start at [`docs/SRS.md`](../docs/SRS.md)
    §6 traceability matrix. Every row has an evidence pointer.
  * **For governance and safety story** — [`docs/RAI.md`](../docs/RAI.md)
    §2.1 autonomy taxonomy and §7 Q&A.
  * **For "how does the production version look?"** —
    [`docs/ROADMAP.md`](../docs/ROADMAP.md), the nine-step ladder.
  * **For the business case** — [`docs/ROI.md`](../docs/ROI.md).
  * **For the recording session** — `demo/DEMO_SCRIPT.md` (local; not in
    the repo per prior instruction).
  * **For hostile-judge rehearsal** — `demo/QA_PREP.md` (local; not in the
    repo per prior instruction).

Both LOCAL-only files live at:
```
/Users/shahy/Desktop/Incident commander/demo/DEMO_SCRIPT.md
/Users/shahy/Desktop/Incident commander/demo/QA_PREP.md
```
