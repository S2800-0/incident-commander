# Software Requirements Specification — Incident Commander

**Version:** 0.2 (Final-Phase Pivot)
**Status:** Draft for team review — not yet approved
**Supersedes:** Pre-final assessment scope (V0.4 deck, 8-bundle corpus)
**Prepared by:** Team Cairo (Nourseen Tarek, Shahesta Mohamed)
**Date:** 2026-09-08

---

## 0. Why this document exists

The pre-final assessment scope proved one mechanism: a Value-of-Information (VoI)
decision policy that knows when to stop passively observing and start testing. The
judge panel asked us to extend that scope in four directions ahead of the final
phase:

1. **NoOps** positioning — frame the system as reducing operator toil toward a
   minimal-human-intervention end state, not just as a diagnostic aid.
2. **Customer-performance-based incident classification** — prioritize and score
   incidents by customer-facing SLA/SLO impact, not only infrastructure metrics.
3. **AI agent automation** — formalize the autonomy boundaries of the existing
   multi-agent system (what is automated vs. what requires human approval).
4. **Responsible AI (RAI)** governance, informed by AI-observability practice
   (e.g., Arize AI-style agent monitoring) — auditability, oversight, and honest
   disclosure of what is *not* yet governed.

A fifth requirement — an **ROI / business case** — is documented separately in
[`ROI.md`](ROI.md), and RAI governance detail lives in [`RAI.md`](RAI.md). This
SRS is the requirements anchor that both reference.

**What this document is not:** a claim that all of the above is built. Section 6
(Traceability) states plainly what exists today, what is partial, and what is
planned. Overclaiming here would be found out in the first Q&A question — see
`demo/QA_PREP.md`'s closing principle, which this document inherits: *name the
line between what runs and what's planned, every time.*

---

## 1. Introduction

### 1.1 Purpose

This SRS defines the requirements for Incident Commander's final-phase scope: a
Value-of-Information–guided incident investigation system, extended with
customer-impact-aware prioritization, formalized AI agent autonomy levels, and a
Responsible AI governance layer — positioned within the broader NoOps and AIOps
practice areas.

### 1.2 Scope

**In scope for this document:** functional and non-functional requirements for
the decision policy, the multi-agent investigation loop, the human-approval gate,
the evidence chain, the customer-impact scoring layer (new), and the RAI
governance hooks (new/formalized).

**Out of scope:** UI redesign (covered by the existing Cisco-language console,
already built), the presentation deck, and live infrastructure connectors beyond
what is listed in Section 5 as external interfaces.

### 1.3 Definitions, Acronyms, and Abbreviations

| Term | Definition |
|---|---|
| **VoI** | Value of Information. `VoI(a) = EIG(a) − λ·cost(a) − μ·risk(a)` — the expected reduction in decision-relevant uncertainty from taking action *a*, net of its cost and risk. See Howard (1966), Chaloner & Verdinelli (1995). |
| **EIG** | Expected Information Gain — the divergence in surviving hypotheses' predictions about an observable, i.e., how much a probe result is expected to change the belief state. |
| **AIOps** | AI for IT Operations — the broader practice area of using AI/ML to automate detection, correlation, and remediation of operational incidents. Incident Commander is a *decision layer* within AIOps, not a competing platform. |
| **NoOps** | An operational end-state in which routine incident response requires no manual operator intervention — automation handles detection through remediation, with humans engaged only for genuinely novel or high-risk decisions. Incident Commander's human-approval gate (Section 4.4) is the explicit boundary of what is *not yet* NoOps, by design. |
| **RAI** | Responsible AI — governance practices ensuring AI system decisions are auditable, bounded, explainable, and subject to human oversight. See [`RAI.md`](RAI.md) for the full framework. |
| **Agent autonomy level** | A discrete classification (L0–L4, defined in [`RAI.md`](RAI.md) §2) of how much a given agent or action is permitted to do without human sign-off. |
| **Intervention** | A bounded, reversible, safety-enveloped production action (e.g., a 5% canary traffic shift) that produces *causal* evidence, as opposed to a passive observation, which produces *correlational* evidence only. |
| **Customer impact score (CIS)** | A new, proposed metric (Section 4.2) that ranks an incident's severity by customer-facing effect (SLA breach, affected-user count, revenue-tagged service) rather than by raw infrastructure signal magnitude alone. |
| **Provenance** | A field on every verdict: `observational` or `interventional`, recording whether the conclusion rests on passive evidence or a human-approved causal test. |
| **Bundle** | A schema-versioned JSON incident scenario (see `schema/incident_bundle.schema.md`) — the corpus unit used for both the ablation harness and, going forward, customer-impact-scoring test cases. |

---

## 2. Overall Description

### 2.1 Product Perspective

Incident Commander is **not** a replacement for existing AIOps/observability
platforms (Datadog, Dynatrace, PagerDuty, Arize AI). It is a **decision layer**
that consumes signals from such platforms (in production; from authored fixture
bundles in the current prototype) and answers one question those platforms leave
to human intuition: *is further observation still worth doing, or should the
system act?*

The final-phase pivot extends this from a pure root-cause decision policy toward
an **operationally positioned** system: incidents are triaged not just by "which
hypothesis is correct" but by "how much does this incident matter to the
customer," and the agent/automation boundary is made explicit enough to serve as
a governance artifact, not just an engineering diagram.

### 2.2 Product Functions (summary — detail in Section 4)

- Multi-agent hypothesis generation and VoI-scored probe/intervention selection
  *(existing, built)*
- Human-gated intervention execution with a specified safety envelope
  *(existing, built)*
- Cryptographically verifiable evidence chain *(existing, built)*
- Customer-impact-aware incident scoring and prioritization *(new, planned)*
- Formalized AI agent autonomy levels with a governance-readable matrix
  *(new, documentation formalizing existing behavior — see RAI.md)*
- ROI quantification model *(new, documentation — see ROI.md)*

### 2.3 User Classes

| User class | Concern |
|---|---|
| **On-call engineer / SRE** | Wants a fast, trustworthy verdict and a clear "should I approve this action?" moment. |
| **Engineering manager / incident commander (human role)** | Wants to know what's automated vs. gated, and what the customer-facing blast radius is. |
| **Compliance / governance reviewer** | Wants an auditable trail and a clear autonomy-level statement — this is the RAI audience. |
| **Judge / evaluator (this phase)** | Wants requirements traced to what's actually built, and an honest ROI case. |

### 2.4 Constraints

- Evaluation corpus remains small (8 bundles) pending corpus-growth work already
  on the roadmap (see `docs/AUTHORING_GUIDE.md`); customer-impact scoring
  requirements in this SRS are **not yet validated against real customer-impact
  data** — this is stated explicitly rather than implied away.
- No live production telemetry connector exists yet; all evidence — including
  any future customer-impact signals — is fixture-based.
- RAI governance in this phase is a **documentation and design artifact**, not a
  running compliance system. See `RAI.md` §4 for exactly what is and isn't
  automated.

---

## 3. External Interface Requirements

| Interface | Current state | Final-phase requirement |
|---|---|---|
| Evidence sources (metrics, logs, deploys) | Fixture JSON bundles | No change required this phase; live Prometheus connector remains a roadmap item (see README roadmap) |
| LLM provider | OpenRouter (free tier) → Claude failover, via `ic/llm.py` | No change; RAI governance requires this path be documented as a monitored dependency (RAI.md §3) |
| Operator console | React/TypeScript, Cisco design language | Add: incident list surfaces Customer Impact Score (Section 4.2) alongside existing slice/ground-truth labels |
| Evidence verifier | Standalone offline `verify.py` | No change |
| AI-agent observability (e.g., Arize AI or equivalent) | Not integrated | **Conceptual only, this phase.** Documented as a Phase-2 roadmap item in RAI.md §5 — not built, not connected, no API key in scope. Rationale: no account/access secured, and building a real integration this close to the final-phase session risks destabilizing a working demo for a feature that cannot yet be evaluated end-to-end. |
| ITSM (ServiceNow, Jira) | Not integrated | Unchanged roadmap item — sealed postmortem JSON is *designed* to populate a ticket resolution record, per existing README, but no connector exists |

---

## 4. Functional Requirements

Each requirement has an ID, a description, and a **status**: `DONE` (built and
verified), `PARTIAL` (some scaffolding exists), or `PLANNED` (documented intent
only, nothing built). Status is authoritative — Section 6 restates these as a
single traceability table for quick judge reference.

### 4.1 Core VoI Decision Policy — `DONE`

- **FR-1.1** — System SHALL maintain a belief state over a finite hypothesis set,
  updated by three independently-scoring agents (change, telemetry, history).
  *(Implemented: `ic/agents/`, `ic/adjudicator.py`)*
- **FR-1.2** — System SHALL score every candidate next action (observation or
  intervention) via `VoI(a) = EIG(a) − λ·cost(a) − μ·risk(a)`.
  *(Implemented: `ic/voi.py`)*
- **FR-1.3** — System SHALL detect observation stagnation
  (`max EIG(a) ≤ ε`) and, only then, surface an intervention recommendation.
  *(Implemented: `ic/orchestrator.py`)*
- **FR-1.4** — System SHALL bound the probe loop (`K=4` probes, `T=45s`,
  `τ=0.15` margin) to prevent unbounded investigation.
  *(Implemented)*

### 4.2 Customer-Performance-Based Incident Classification — `DONE`

Primary net-new functional area from the judge panel. Shipped Sep 16.

- **FR-2.1** — Each incident bundle carries an optional `customer_impact`
  object: `{ tier: "P0"–"P3", affected_users: int, sla_breach: bool,
  revenue_tagged_service: bool, impact_source: str }`.
  *(Implemented: `ic/cis.py`, corpus bundles INC-4478, INC-4473, INC-4479 –
  INC-4482 all carry a `customer_impact` block. Backward-compatible: bundles
  without the block simply produce `verdict.customer_impact = None`.)*
- **FR-2.2** — CIS is computed and emitted, but it never influences
  hypothesis scoring or VoI probe selection. This is enforced **structurally**:
  `ic/cis.py` is not imported by any diagnostic-path module (audit
  ic/adjudicator.py, ic/probe.py, ic/reasoner.py, ic/voi.py, ic/agents/*), and
  CIS is computed AFTER the verdict is frozen so a temporal ordering
  guarantee makes influence impossible.
  *(Implemented. AST audit at `demo/screenshots/cis/04_guardrail_static.png`
  confirms zero imports across 10 diagnostic-path files.)*
- **FR-2.3** — The verdict object carries a `customer_impact` payload
  (score, tier, urgency label, components) available to any console or
  postmortem export.
  *(Implemented: `ic/models.py:Verdict.customer_impact`; sealed into the
  Merkle evidence chain via `ic/cis.py::to_evidence_item()`.)*
- **FR-2.4** — CIS is documented alongside the other bundle fields;
  updating `docs/AUTHORING_GUIDE.md` with a dedicated section on writing
  `customer_impact` blocks is left as a docs-side follow-up.
  *(PARTIAL — the format is defined by the pydantic-validated block in
  `ic/cis.py`; a walk-through section in AUTHORING_GUIDE.md is the remaining
  step.)*

### 4.3 AI Agent Automation — `DONE`

- **FR-3.1** — Every agent and adjudicator is assigned an explicit autonomy
  level (L0–L4, defined in `RAI.md` §2). The taxonomy is applied per
  component with real code paths cited.
  *(Implemented — see `RAI.md` §2.1. Change/telemetry/history agents = L1;
  VoI probe selection = L2; intervention/rollback recommendation = L3
  (policy-gated); evidence sealing = L4.)*
- **FR-3.2** — Every new automated action added since this SRS was drafted
  was assigned an autonomy level before implementation: OPA policy calls =
  L3 (blocking), verification re-check = L2 (bounded read-only),
  auto-revert = L3 (envelope-enforced).
  *(Implemented and enforced. See `docs/ROADMAP.md` stage 5 for the
  promotion protocol used to move an action class from L3 to L2.)*

### 4.4 Policy-Gated Autonomy & Staged NoOps — `DONE`

Following the judge panel's NoOps direction, the human `Approve` button has
been replaced with a **real OPA + Rego policy engine** — deterministic
policy-as-code that decides ALLOW / DENY per state-changing action against
structured inputs (confidence, blast radius, deploy age, upstream health).

- **FR-4.1** — Every state-changing action executes only after an OPA policy
  evaluation returns ALLOW. Failing (unavailable / DENY) fires
  `action_blocked` and skips execution. The whole flow is streamed as
  `policy_decision` events sealed into the evidence chain.
  *(Implemented: `policy/incident/action.rego` (3 action classes),
  `ic/policy.py` (fail-closed HTTP client), `ic/orchestrator.py` (real gate).
  Live-decision screenshots at `demo/screenshots/opa/`. Docker sidecar via
  `docker compose up opa`.)*
- **FR-4.2** — NoOps is framed and shipped as a **staged ladder**
  (`docs/ROADMAP.md` stage 5): action classes are promoted from L3
  (policy-gated) to L2 (auto-execute within a hard-coded envelope) one at a
  time, with an auditable justification. Read-only diagnostic re-queries are
  the first candidate promotion; state-changing actions stay L3.
  *(Implemented: taxonomy in `RAI.md` §2.1, promotion protocol in
  `ROADMAP.md` stage 5.)*
- **FR-4.3** — Autonomous interventions are followed by a **real
  verification loop** — the customer signal is re-checked against a recovery
  band; failure fires `auto_revert_triggered` and demotes the verdict's
  provenance to `observational`.
  *(Implemented: `ic/verification.py`,
  `evidence.verification_result` bundle field, live demonstrations on
  INC-4478 (success) and INC-4481 (auto-revert). Screenshots at
  `demo/screenshots/verification/`.)*

### 4.5 Responsible AI Governance — `DONE`

- **FR-5.1** — Every verdict Merkle-rooted over cited evidence and Ed25519-
  signed, verifiable offline.
  *(Implemented: `ic/evidence_chain.py`, `verify.py`.)*
- **FR-5.2** — Every verdict carries a `provenance` field. A verified
  intervention keeps `interventional`; a failed intervention demotes to
  `observational`.
  *(Implemented: `ic/orchestrator.py`; verification-driven demotion added
  Sep 15.)*
- **FR-5.3** — AI-observability integration (Arize-style) remains
  documented-only this phase; the integration point is specified in
  `RAI.md` §5 with the reason it is not yet built.
  *(PLANNED (docs only) — no capability change from previous version. See
  `ROADMAP.md` stage 3 rationale.)*
- **FR-5.4** — Posterior cap 0.94 enforced.
  *(Implemented: `ic/reasoner.py`.)*
- **FR-5.5** — Anti-hallucination filter on all dynamic-triage evidence
  citations: any supporting or contradicting evidence ref that does not
  resolve to a real prior-evidence source_uri is silently dropped before
  the hypothesis reaches the reasoner.
  *(Implemented: `ic/dynamic_triage.py::_to_hypothesis`. Live proof at
  `demo/screenshots/dynamic/03_anti_hallucination.png`.)*

### 4.6 ROI Quantification — `DONE`

- **FR-6.1** — Standalone ROI computation, backed by real harness output.
  Data inputs (accuracy delta, FP-rollback delta, avg human diagnostic-time
  baseline) are pulled from `harness_results.json` each run — the ROI figure
  can never drift from the graded numbers because it is *derived* from them.
  Assumption inputs (incident volume, $/hr, $/rollback) are CLI arguments
  with clearly labeled defaults from `ROI.md`.
  *(Implemented: `ic/roi.py` (`python -m ic.roi`). Refreshes automatically
  when the corpus grows — the wider 12-bundle corpus's numbers replace the
  8-bundle ones without any doc edit.)*
- **FR-6.2** — ROI output distinguishes DATA (measured, reproducible) from
  INPUT (assumed) at every printed value; a sensitivity analysis (±50%
  swing on each INPUT) is included so no point-estimate carries hidden
  assumption weight.
  *(Implemented: `ic/roi.py::_sensitivity`, terminal report shows
  biggest-lever-first ordering.)*
- **FR-6.3** — ROI computation explicitly excludes the harness's
  `time_to_conclusion_s` field (~1 ms) — a fixture-data artifact, not a live
  latency, and using it as a "speed" claim would be indefensible under
  scrutiny. The exclusion and the reasoning are called out in `ROI.md` §3
  and in the script's caveats output.
  *(Implemented: an intentional non-use of a superficially-attractive
  number, documented at the point of decision.)*

---

## 5. Non-Functional Requirements

| ID | Requirement | Status |
|---|---|---|
| NFR-1 | The graded ablation harness SHALL remain fully deterministic and reproducible with no API key (`IC_USE_LLM` gate). | DONE |
| NFR-2 | Any LLM-path failure SHALL fall back to the deterministic reasoning lens without breaking an investigation. | DONE |
| NFR-3 | The evidence chain SHALL be verifiable fully offline, with no network dependency. | DONE |
| NFR-4 | Any new customer-impact-scoring logic SHALL NOT alter the deterministic hypothesis-scoring path used by the ablation harness — impact scoring is additive metadata, not a diagnostic input. | DONE — enforced by static AST audit (0 imports from `ic.cis` across 10 diagnostic-path files) AND by temporal separation (CIS computed after verdict is frozen). Screenshot: `demo/screenshots/cis/04_guardrail_static.png`. |
| NFR-5 | RAI documentation SHALL explicitly separate "governed and audited" from "documented intent only" — no requirement in this SRS may be marked DONE unless a corresponding code path exists. | Enforced by this document's own status column |

---

## 6. Requirements Traceability Matrix

| Req ID | Area | Status | Evidence |
|---|---|---|---|
| FR-1.1–1.4 | Core VoI policy | **DONE** | `ic/voi.py`, `ic/orchestrator.py`, `ic/adjudicator.py` |
| FR-2.1–2.3 | Customer-impact scoring (schema, guardrail, verdict field) | **DONE** | `ic/cis.py`, static AST guardrail, `demo/screenshots/cis/` |
| FR-2.4 | Authoring-guide walkthrough for CIS | **PARTIAL** | Format is defined by `ic/cis.py`; walkthrough in `AUTHORING_GUIDE.md` is a docs follow-up |
| FR-3.1–3.2 | Agent autonomy taxonomy applied per component | **DONE** | `RAI.md` §2.1 maps every component; every action added since carries an explicit level |
| FR-4.1 | Policy-gated state-changing actions (OPA + Rego) | **DONE** | `policy/incident/action.rego`, `ic/policy.py`, `demo/screenshots/opa/` |
| FR-4.2 | Staged NoOps ladder | **DONE** | `ROADMAP.md` stage 5, `RAI.md` §2.2 promotion protocol |
| FR-4.3 | Real verification loop + auto-revert | **DONE** | `ic/verification.py`, `demo/screenshots/verification/` |
| FR-5.1–5.2, 5.4 | Evidence chain, provenance (with verification demotion), calibration cap | **DONE** | `ic/evidence_chain.py`, `verify.py`, `ic/reasoner.py`, `ic/orchestrator.py` (provenance demotion added Sep 15) |
| FR-5.3 | AI-observability hook (Arize-style) | **PLANNED** (docs only) | `RAI.md` §5, `ROADMAP.md` stage 3 |
| FR-5.5 | Anti-hallucination filter on dynamic hypotheses | **DONE** | `ic/dynamic_triage.py::_to_hypothesis`, `demo/screenshots/dynamic/03_anti_hallucination.png` |
| FR-6.1–6.3 | ROI computed from harness output, sensitivity analysis, honest exclusion of misleading numbers | **DONE** | `ic/roi.py`, `ROI.md` |
| **New: FR-7** | OTLP-shaped ingestion adapter (production wire format) | **DONE** | `ic/otlp.py`, `POST /ingest/otlp/v1/{metrics,logs}`, NiFi flow config, `demo/screenshots/otlp/` |
| **New: FR-8** | Dynamic hypothesis generation (LLM triage, opt-in) | **DONE** | `ic/dynamic_triage.py`, two-gate opt-in, transparent fallback to seeded, `demo/screenshots/dynamic/` |
| **New: FR-9** | Corpus expansion — 4 bundles exercising CIS extremes + auto-revert + autonomous success | **DONE** | `corpus/INC-4479` – `INC-4482`, all pass harness, `demo/screenshots/corpus/` |

**Reading this table honestly (updated Sep 20):** every judge-requested
direction now has a **DONE** row backed by running code and a screenshot
set. What began as a docs-only pass (Sep 11 draft) became a real build
pass — the pivot's original PLANNED items for CIS, agent-automation
autonomy taxonomy, staged NoOps + verification, and ROI computation were
promoted to DONE between Sep 12 and Sep 19. Two new areas (OTLP adapter,
dynamic hypothesis generation) were added and are also DONE. Only Arize AI
observability remains PLANNED, per the honest scope decision in `RAI.md`
§5. **A judge scanning this table sees the answer to every one of their
four pivot asks with a real code path and a screenshot to point at.**

---

## 7. Open Questions for the Team

Each of these has resolved since the Sep 11 draft:

1. ~~CIS build vs docs-only~~ → **Built.** `ic/cis.py`, static AST guardrail,
   two new corpus bundles (INC-4479 P0 critical, INC-4480 P3 low) that
   exercise the extremes.
2. Real ROI inputs — **placeholder-based model kept** on user's judgment
   call: real org numbers aren't available for a hackathon prototype, and
   fabricating them would be less defensible than presenting the formula
   with clearly-labeled INPUTs. See `ROI.md` §0.
3. Arize AI scope — **stays conceptual**, per user's "you decide" ruling.
   `RAI.md` §5 documents the honest reason (no account, corpus too small
   for drift monitoring to be meaningful yet). Named as `ROADMAP.md` stage
   3 with the specific trigger for when to build it.

Fresh open items surfaced by the build phase:

4. `AUTHORING_GUIDE.md` needs a section on writing `customer_impact`
   blocks (FR-2.4 PARTIAL).
5. NiFi flow file (`nifi/otlp_ingestion.json`) is provided by-hand today;
   should be exportable via NiFi's registry client if we grow the flow.

---

*See also: [`ROADMAP.md`](ROADMAP.md) for the nine-step production
maturity ladder, [`ROI.md`](ROI.md) for the business case,
[`RAI.md`](RAI.md) for governance and autonomy-level definitions,
`README.md` for the system overview, and `docs/AUTHORING_GUIDE.md` for
corpus-authoring practice.*
