# Incident Commander — Production Maturity Roadmap

**Version:** 1.0 (Final-Phase Pivot)
**Companion to:** [`SRS.md`](SRS.md), [`RAI.md`](RAI.md), [`ROI.md`](ROI.md)
**Audience:** judge panel, engineering leadership, RAI reviewer

---

## 0. What this document is

A concrete, staged plan for moving Incident Commander from the working prototype
you see today to a shadow-mode-then-selective-NoOps deployment in a real
production environment. Every stage names:

  * **What ships**  — the specific capability that gets delivered.
  * **How it ships** — the named tool / service, not vague abstractions.
  * **What it proves** — the judge-facing question this stage answers.
  * **Status**        — DONE (visible in this repo today), IN FLIGHT
    (partial, work in progress), or PLANNED (specified only).

**The one rule:** every "DONE" here is backed by running code and a
screenshot in `demo/screenshots/`. Every "PLANNED" is specified with a
concrete tool choice so a future engineer knows exactly what to build.

---

## 1. Where we are today — the baseline

| Component                              | Status | Evidence |
|----------------------------------------|--------|----------|
| Multi-agent triage                     | DONE   | [`ic/agents/`](../ic/agents), `agent_reasoned` events |
| VoI-guided probe adjudicator           | DONE   | [`ic/voi.py`](../ic/voi.py), `voi_scored` events |
| Bayesian belief update                 | DONE   | [`ic/adjudicator.py`](../ic/adjudicator.py) |
| Cryptographic evidence chain           | DONE   | [`ic/evidence_chain.py`](../ic/evidence_chain.py), `chain_sealed` events |
| Ablation harness (100%/0% headline)    | DONE   | [`ic/harness.py`](../ic/harness.py), reproducible without any external service |
| **OPA + Rego policy engine (real)**    | DONE   | [`policy/incident/action.rego`](../policy/incident/action.rego), [`ic/policy.py`](../ic/policy.py), Docker sidecar |
| **OTLP ingestion adapter (spec)**       | DONE   | [`ic/otlp.py`](../ic/otlp.py), `POST /ingest/otlp/v1/{metrics,logs}` on the server |
| **Verification loop**                   | DONE   | [`ic/verification.py`](../ic/verification.py), `verification_result` + `auto_revert_triggered` events |
| **Customer-Impact Scoring (CIS)**       | DONE   | [`ic/cis.py`](../ic/cis.py) + static guardrail (0 diagnostic-path imports) |
| **Dynamic hypothesis generation**       | DONE   | [`ic/dynamic_triage.py`](../ic/dynamic_triage.py), opt-in via IC_DYNAMIC_HYPOTHESES=1 |
| **Real ROI computation**                | DONE   | [`ic/roi.py`](../ic/roi.py) reads harness output, computes both drivers |
| Corpus (12 bundles, 5 slices)          | DONE   | [`corpus/`](../corpus) |
| Console (React, Cisco design language)  | DONE   | [`console/`](../console) |

**Twelve components** shipping as running code, every one with a screenshot
set under `demo/screenshots/<feature>/`.

---

## 2. The nine-step ladder

### Stage 1 — Connect real telemetry via OpenTelemetry

**Ships:** an OTLP Collector alongside NiFi ingestion, replacing the corpus
bridge as the OTLP source. Semantic conventions for customer-facing SLIs
(checkout success, payment success, order latency).

**How:** OpenTelemetry Collector → NiFi flow (already in
[`nifi/README.md`](../nifi/README.md)) → `POST /ingest/otlp/v1/...` (already
implemented — the adapter accepts real OTLP JSON today).

**What it proves:** we haven't just built against fixtures; the ingestion
contract IS the production one.

**Status:** IN FLIGHT.
  * Adapter: DONE (spec-compliant on both metrics and logs).
  * NiFi flow file: DONE (importable, `docker compose --profile ingestion up nifi`).
  * Live Collector wiring: PLANNED — awaits the target environment's
    Collector and the customer signals to instrument first.

---

### Stage 2 — Dynamic hypothesis generation

**Ships:** live LLM triage that generates 2-4 competing hypotheses from raw
incident context, each carrying claim + supporting evidence + contradicting
evidence + predictions + discriminating signals + confidence.

**How:** [`ic/dynamic_triage.py`](../ic/dynamic_triage.py) — two-gate opt-in
(`IC_USE_LLM=1` + `IC_DYNAMIC_HYPOTHESES=1`), pydantic-validated schema,
anti-hallucination filter drops invented evidence refs.

**What it proves:** the mechanism is not shackled to a hand-authored corpus
— it generalises to genuinely unseen incidents by construction.

**Status:** DONE. Coexists with seeded hypotheses (harness explicitly opts
out to preserve reproducibility).

---

### Stage 3 — Dynamic probe generation + formal EIG

**Ships:** probe catalogue expanded at runtime from the hypothesis
predictions ("H1 predicts A, H2 predicts B — find an observable that
distinguishes A/B"), scored by information-theoretic Expected Information
Gain (EIG) rather than the current divergence heuristic.

**How:** extend [`ic/voi.py`](../ic/voi.py) with a formal EIG estimator
(Chaloner & Verdinelli, 1995 style), add an LLM-driven catalogue generator
that reads a discovery service (Prometheus/OTel resource catalogue) and
returns candidate PromQL / LogQL queries.

**What it proves:** the "select the check the remaining stories disagree
about" claim is a formal decision under uncertainty, not a heuristic.

**Status:** PLANNED. Highest-value single item on the ladder that is not
already IN FLIGHT — worth a dedicated engineering sprint.

---

### Stage 4 — Policy-as-code before any autonomy

**Ships:** every state-changing action goes through OPA/Rego evaluation before
execution. The mechanism can propose, but the policy engine decides ALLOW /
DENY / REQUIRE_APPROVAL against structured inputs (confidence, blast radius,
deploy age, upstream health).

**How:** [`policy/incident/action.rego`](../policy/incident/action.rego)
today with three action classes (rollback, canary, intervention). Docker
sidecar (`docker compose up opa`), fail-closed on unavailable engine,
decisions logged into `decision_logs.console` for audit.

**What it proves:** an AI cannot "decide it's OK to touch production."
Policy is separated from mechanism, deployed and versioned independently.

**Status:** DONE. Live decisions verifiable via
`demo/screenshots/opa/03_scenarios.png`.

---

### Stage 5 — Staged intervention (read-only → recommend → approve → auto-low-risk → canary → selective NoOps)

**Ships:** the autonomy ladder from RAI L0 → L4, applied per action class.
The floor is L0 (agents propose, don't act); the ceiling is L4 (fully
autonomous within a hard-coded envelope). What changes per stage is *which
action classes* live at *which levels*.

**How:** the RAI taxonomy in [`RAI.md`](RAI.md) §2.1 maps every current
component to its level today. The next promotion candidate is
"read-only diagnostic re-queries" from L3 → L2 (§2.2), because they carry no
state change.

For live-production canary deployments in a Kubernetes environment: **Argo
Rollouts** as the execution controller — it handles canary/blue-green
workflows and uses metrics to drive promotion or rollback, which matches
Incident Commander's decision + verification loop exactly.

**What it proves:** NoOps is not "no humans"; it is "no humans required for
this specific, well-understood, safe action class."

**Status:** IN FLIGHT. Envelope enforcement + verification loop shipped
(see stage 6); Argo Rollouts integration PLANNED.

---

### Stage 6 — Verification (measurable recovery, not just API success)

**Ships:** every intervention is followed by a real check against the
customer signal. `recovered=True` requires the signal returning to a
recovery band; `recovered=False` triggers `auto_revert_triggered` and demotes
the verdict to `observational`.

**How:** [`ic/verification.py`](../ic/verification.py), sealed as an evidence
leaf in the Merkle+Ed25519 chain. Bundle-side spec:
`evidence.verification_result` — signal, baseline, post-intervention value,
band.

For durable long-running verification workflows (an incident that takes
hours to resolve): **Temporal** as the workflow engine — it survives
process restarts, network failures, and provides deterministic replay for
the audit trail.

**What it proves:** "the API call succeeded" ≠ "the incident is fixed."
The system will never claim recovery it cannot measure.

**Status:** DONE (in-process verification loop). Temporal durability
PLANNED — becomes essential when incidents span hours rather than seconds.

---

### Stage 7 — Improved agent architecture (Safety + Verification agents as first-class)

**Ships:** two additional roles alongside change/telemetry/history:

  * **Safety agent** — the OPA/policy call becomes a first-class agent
    contribution in the event stream (already true — `policy_decision`
    events are already indexed alongside `agent_reasoned`).
  * **Verification agent** — the recovery check becomes a first-class
    contribution too (already true — `verification_started` /
    `verification_result` are streamed).

Deterministic code (policy, execution, audit) stays separate from LLM
interpretation (agents). This matches the reviewer's rule: "the LLM should
mostly do interpretation/reasoning. Deterministic code should control
policies, execution, audit."

**Status:** DONE in the event stream shape; explicit agent-role naming in
the console UI is PLANNED (a UI-side polish item).

---

### Stage 8 — Held-out evaluation on unseen incidents

**Ships:** evaluation on bundles the mechanism has never seen the ground
truth for, measuring beyond top-1 accuracy: calibration (Brier already
computed), steps-to-separation, wasted probes, false-positive remediation,
verified-recovery rate, customer-impact-weighted accuracy.

**How:** the corpus (12 bundles today) grows via `docs/AUTHORING_GUIDE.md`
authored from real postmortems. Held-out slicing already exists via the
`slice` field (`ambiguous`, `adversarial_redherring`, etc.); the harness
already reports per-slice metrics.

**What it proves:** the mechanism generalises past the incidents it was
tuned on — a defensible answer to "does this work on new problems?"

**Status:** IN FLIGHT. Structure is there (see harness output);
scaling to 30+ bundles authored from real postmortems is the roadmap
item — matched to the pace of finding postmortems the team has consent to
use.

---

### Stage 9 — Shadow mode → selective NoOps rollout

**Ships:** the mechanism runs against every real incident, produces a
verdict + recommendation + (for high-CIS incidents) a proposed action, but
does not execute. The on-call engineer compares its recommendation to
their own. Once shadow-mode performance is proven for a specific incident
class (deployment-induced 5xx spikes with clean sibling checks, say), that
class is promoted to auto-execute within the OPA envelope for low-risk
bounded actions.

**How:** a `shadow_mode: true` orchestrator flag (~10 lines of code) that
short-circuits execution while logging the decisions as `would_have_executed`
events. Promotion from shadow → selective NoOps is a policy-side change,
not a mechanism change — flip the Rego rule for that action class.

**What it proves:** the credibility bridge between "prototype we tuned on
authored bundles" and "system we let touch production." This is the
industry-standard path for AI-in-production, per every recent AI-SRE
paper.

**Status:** PLANNED. The mechanism is already gated; adding the
short-circuit is deferred pending a live-incident source (Stage 1).

---

## 3. Ordering — what to build next, in priority sequence

For a team continuing this work post-hackathon, the honest priority order
is:

  1. **Stage 1 completion** — wire a live OTel Collector to NiFi. Everything
     else is speculative without live signal.
  2. **Stage 8 expansion** — grow the corpus from real postmortems.
     Calibration numbers need volume to be defensible.
  3. **Stage 9 shadow mode** — small code change, big credibility jump for
     the "have you tested against real incidents" question.
  4. **Stage 3 formal EIG** — the mechanism gets a lot stronger; also
     replaces the current heuristic, so it's a clean upgrade path.
  5. **Stage 5 Argo Rollouts** — actual production automation once shadow
     mode has run against enough real incidents.
  6. **Stage 6 Temporal** — becomes essential once incidents span hours.

Everything else on the reviewer's original list (Kafka/Redpanda, pgvector,
Redis-based session store) is genuinely useful in a production build but is
scaffolding around the mechanism, not the mechanism itself. It comes later.

---

## 4. Tech-stack decisions — with substitutions

Documented in [`SRS.md`](SRS.md) §3, mirrored here for a single-glance view:

| Layer                       | Advisor's choice | Our stage                              |
|-----------------------------|------------------|----------------------------------------|
| Frontend                    | React + TS + Vite | DONE (`console/`)                      |
| Backend                     | Python + FastAPI  | DONE (`server/`)                       |
| Policy engine               | OPA + Rego        | DONE (Stage 4)                         |
| Telemetry ingestion         | OpenTelemetry     | DONE adapter (Stage 1) — Collector PLANNED |
| Ingestion routing           | (implicit)        | Apache NiFi — DONE (`nifi/`)            |
| Async / event bus           | Kafka / Redpanda  | PLANNED — no capability need in prototype |
| Durable workflows           | Temporal          | PLANNED (Stage 6)                      |
| Progressive delivery        | Argo Rollouts     | PLANNED (Stage 5)                      |
| Metrics / logs / traces     | Prometheus + APM  | PLANNED — behind the NiFi ingest boundary |
| Primary DB                  | Postgres          | Cassandra substituted (append-only audit) |
| Historical retrieval        | pgvector          | PLANNED                                |
| Cache / gate state          | Redis             | PLANNED (backend gate durability)      |
| Execution                   | Kubernetes        | PLANNED (Stage 5 lands with Argo)      |
| LLM layer                   | pluggable         | DONE (`ic/llm.py` — OpenRouter + Anthropic failover) |

**Rule enforced across every substitution:** if we chose a different tool,
the reason is in the row (usually: "no capability gained for prototype
demo, adds moving parts").

---

## 5. What we DELIBERATELY did not build

Two categories, both important to name honestly:

**Category A — deferred for demo integrity, not for capability reasons:**
  * Kafka / Redpanda — no async ordering guarantee needed for 45-second
    demo. Named in stage 6 for durability.
  * Temporal — same reasoning.
  * Argo Rollouts — needs a real k8s cluster; adds live-demo failure
    modes. Named for stage 5.
  * Live-incident shadow mode — no real-incident source in a hackathon
    environment. Named for stage 9.

**Category B — outside the honest scope of what "final phase" can deliver:**
  * A large-scale evaluation against a public postmortem corpus. This is a
    research paper's worth of work; we say so.
  * A production-hardened AI-observability integration (Arize AI or
    equivalent) — 8-bundle corpus is too small for drift monitoring to be
    meaningful, and building an integration we cannot exercise would be
    theatre. See `RAI.md` §5 for the honest scope decision.

Both categories are treated the same way by every judge-facing document:
label as PLANNED, name the tool, name the trigger condition that would
change our mind about deferring.

---

## 6. Summary — what a judge can point at

If a judge asks "what does the production version of this look like?", the
answer is:

> *"This document, mapped onto our repo. Every DONE row is a running
> component you can see in `demo/screenshots/`. Every PLANNED row names the
> specific tool we'd use (OTel Collector, Argo Rollouts, Temporal, Kafka,
> Arize) and the specific reason it isn't shipped yet. The stages are
> ordered — we know what comes next and why."*

That is the roadmap. It answers the reviewer's original nine-point plan
directly, section by section, with honest labels and running code where
we could ship it.
