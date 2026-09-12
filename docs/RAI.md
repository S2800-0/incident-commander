# Responsible AI (RAI) Governance — Incident Commander

**Version:** 0.1 (Final-Phase Pivot)
**Status:** Updated Sep 20 — reflects the shipped policy engine, verification loop, and CIS guardrail. See section 8 for the version-history change list.
**Companion to:** [`SRS.md`](SRS.md) (requirements FR-3.x, FR-4.2, FR-5.x)

---

## 0. What this document is

A governance framework for the AI components of Incident Commander: what each
agent is permitted to decide, what always requires a human, what is audited,
and — critically — what is **not yet** governed. This is the document a
compliance reviewer or a skeptical judge would ask for.

**The one rule this document follows:** every claim here is either backed by a
code path you can point to, or explicitly labeled as design intent / roadmap.
No claim is allowed to sit ambiguously between the two.

---

## 1. Why RAI matters for this system specifically

Incident Commander's core mechanism — VoI-guided investigation with a
human-gated intervention — is *already* a Responsible AI pattern, even though it
predates this document. The system was designed from the start to:

- Never let an AI agent take a state-changing action (Section 3)
- Never report false certainty (posterior cap, Section 4)
- Never let unverified evidence enter the decision without a hash and a source
  (Section 5)

This document formalizes what was implicit, adds the governance vocabulary a
judge or a compliance stakeholder expects, and names the gaps honestly.

---

## 2. Agent Autonomy Levels

A five-level scale (L0–L4), adapted from common AI-autonomy taxonomies, applied
to every component in the system.

| Level | Name | Definition | Human role |
|---|---|---|---|
| **L0** | No autonomy | Component only reads/reports; makes no decision | Human makes every decision |
| **L1** | Assisted | Component proposes; human must act on the proposal | Human decides, AI informs |
| **L2** | Supervised automation | Component decides *and acts*, but only within a pre-approved, bounded, reversible envelope, and the action is logged for review | Human sets the envelope in advance; reviews after the fact |
| **L3** | Gated automation | Component decides and *would* act, but execution is blocked pending a real-time human approval click | Human approves each instance, in the moment |
| **L4** | Full autonomy | Component decides and acts with no human step, bounded only by hard-coded safety limits | Human sets limits once; no per-instance involvement |

### 2.1 Current classification of every component

| Component | Level | Rationale |
|---|---|---|
| **Change / Telemetry / History agents** | **L1** | Each proposes a hypothesis and a confidence score. They never act; the adjudicator and the human downstream decide what happens with their output. |
| **VoI Adjudicator (probe selection)** | **L2** | Selects and executes *observations* (read-only probes) autonomously, within the hard-coded bounds `K=4`, `T=45s`. Observations are non-destructive by construction, so this is bounded automation, not a decision requiring per-instance approval. |
| **VoI Adjudicator (intervention recommendation)** | **L3** | Computes that a bounded intervention (e.g., canary traffic shift) is the highest-value next action, and presents it. Now gated by **real OPA policy evaluation** — the `policy_decision` event carries structured ALLOW/DENY with reasons, sealed into the evidence chain. Policy engine is deployed as a Docker sidecar (`docker compose up opa`), fail-closed on unavailability. This is the system's single most important governance boundary. |
| **Intervention executor** | **L3** — policy-gated | Executes only after OPA returns ALLOW. Once permitted, execution proceeds automatically against the specified safety envelope (5% traffic, 60s, auto-revert). The *decision* to permit is deterministic policy-as-code; the *execution mechanics* after permission are automated within the pre-committed envelope. Failure of the recovery signal fires `auto_revert_triggered` and demotes the verdict provenance to `observational`. |
| **Rollback recommendation** | **L3** — policy-gated | Same policy path; separate Rego rule with distinct preconditions (posterior ≥ 0.75, deploy recent, no upstream outage detected, evidence citations present). |
| **Verification recovery check** | **L2** — bounded read-only | After an intervention, re-checks the customer-facing signal against the bundle's recovery band. Read-only observation of a signal already flowing into IC; no state change. Runs automatically because there is no governance reason for a human to click "yes, check whether it worked." |
| **CIS computation** | **L4** — no decision content | Reads `customer_impact` bundle metadata and computes an urgency score. Structurally prevented from influencing diagnosis (static AST audit: zero imports from `ic.cis` across 10 diagnostic-path files). Same reasoning as evidence sealing — no governance risk in automating a data function. |
| **Evidence sealing (Merkle + Ed25519)** | **L4** | Fully automated by design — no governance reason for a human to approve hashing. No decision content, only integrity guarantees. |

**Design principle stated explicitly:** the only components at L3+ (human gate)
are those whose action is either state-changing (intervention, rollback) or
irreversible in effect on production traffic. Everything upstream of that —
reading, scoring, hashing — is automated, because automating *those* steps
carries no governance risk and is exactly where AIOps automation should focus
first.

### 2.2 What "toward NoOps" means, honestly

NoOps, as a target end-state, means moving *specific, audited, low-risk* action
classes from L3 to L2 over time — never a blanket increase in autonomy.

**One promotion has now been made** (Sep 15): the verification recovery check
was promoted from PLANNED L3 (would have required a human "verify" click) to
shipped L2 (auto-runs, bounded, read-only). Rationale: reading a signal that
is already flowing into IC carries the same non-destructive property as the
initial probe loop. Every promotion follows this exact template — a specific
action class, a named safety property that justifies the promotion, and a
committed audit path.

**Next candidate promotion (not yet made):** re-running a probe that already
executed once, when a signal is transient and needs confirmation. Same
non-destructive property, but currently sits at L2 as part of the initial
probe loop already, so there is no separate promotion needed — this is
called out here because a reader tracking the promotion protocol should be
able to see exactly which classes are candidates and why.

**State-changing actions (intervention, rollback) stay L3.** No plan to
promote them — they cross the state-change boundary, and the policy engine
is the governance surface for that boundary.

---

## 3. Containment: How Untrusted Content Is Prevented From Becoming Action

This is the mechanism that makes L1 agents safe to run against arbitrary
evidence text (which could, in a production system, contain adversarial or
simply misleading content — e.g., a log line an attacker crafted to look like a
legitimate signal).

- **The adjudicator has no tool access.** It only reconciles structured
  `Hypothesis` objects. Evidence text can bias what an *agent* concludes, but
  the adjudicator that makes the probe/intervention decision never executes
  anything an agent's text told it to — it executes only what its own VoI
  scoring computed.
- **Evidence enters LLM context as delimited, typed, hashed data** — never in
  an instruction position (`ic/llm.py::evidence_block`). A prompt-injection
  payload inside evidence text is, structurally, just more evidence text.
- **Every hypothesis must cite a resolvable evidence reference** or its
  posterior is penalized (`NO_EVIDENCE_PENALTY`, `ic/orchestrator.py`) — an
  agent asserting something with no backing evidence cannot win on assertion
  alone.

**Gap, stated honestly:** this containment has not been adversarially tested
against a corpus of deliberately injected evidence. It follows from the
architecture (no tool access, structured-only outputs), but "follows from the
architecture" is not the same as "verified under attack." This is listed as a
roadmap item in Section 6.

---

## 4. Calibration Honesty

- Every posterior is capped below certainty (currently 0.94) — the system is
  structurally prevented from reporting 100% confidence, regardless of how
  lopsided the evidence is.
- Confidence is measured against reality via a reliability diagram (predicted
  vs. empirical accuracy), not just asserted. Corpus size grew to **12 bundles
  (Sep 19)** spanning five slice types — enough to see calibration behavior
  across all four interpreter families; still small enough that we say so
  explicitly rather than call this publication-grade.
- The deterministic engine is the **only** source of graded/reported numbers
  (the ablation harness always runs `use_llm=False`) — so calibration claims
  are reproducible and not subject to LLM run-to-run variance.

---

## 5. AI-Observability Roadmap (Arize AI-style monitoring) — NOT BUILT

The judge panel raised AI-agent observability (an "Arize AI"-adjacent concept)
as a direction. Here is the honest scope decision and why:

**What this would add, conceptually:** continuous monitoring of agent output
quality over time — e.g., is the change-agent's confidence calibration drifting
as new incident types are added to the corpus? Are the three agents' hypothesis
proposals becoming systematically correlated (reducing the value of having
three independent lenses)? Platforms like Arize AI specialize in exactly this
kind of ML/agent-output monitoring in production.

**Why it is not built this phase:**
- No Arize account or API access has been secured.
- The corpus (12 bundles as of Sep 19) is still too small for drift
  monitoring to mean anything — you need enough runs over enough time to
  detect drift, and this system does not yet have production traffic.
- Building a real integration this close to the final-phase session risks
  destabilizing a working demo for a feature that cannot yet be evaluated
  end-to-end.

**What we commit to instead, honestly:** this document specifies the
integration point (Section 6, roadmap) so that when the corpus and production
telemetry exist, the hook is a scoped addition, not an architectural rewrite.

---

## 6. Roadmap — Governance Items Not Yet Built

Updated Sep 20. Items 1 and 3 from the previous version shipped:

  * ~~Formalize CIS as governed metadata with a diagnostic-path guardrail~~
    → Shipped: `ic/cis.py` + static AST audit (§2.1 CIS row).
  * ~~Promote a first read-only action class from L3 to L2~~ → Shipped:
    the verification recovery check is now L2 (§2.2).

Remaining, in priority order:

1. **Adversarial evidence-injection testing** against the containment claims
   in Section 3 — currently architectural, not empirically verified. Highest
   remaining item on this list — the containment argument is load-bearing
   for every other governance claim.
2. **AI-observability integration** (Arize AI or equivalent) once production
   telemetry volume justifies drift monitoring (Section 5). Named as
   `ROADMAP.md` stage 3.
3. **Organization-configurable λ/μ/ε weights**, so risk tolerance for
   interventions can be governed per-deployment rather than hardcoded by
   the development team. Configuration would flow through the same
   `.rego` policy files that already gate the actions.
4. **Console-side surfacing** of the autonomy level of each action taken —
   the event stream already carries the classification; the UI does not yet
   display it prominently.

---

## 7. Summary Table (for a 30-second judge answer)

| Question a judge might ask | Answer |
|---|---|
| "Can the AI take a destructive action on its own?" | No. Every state-changing action is L3, gated by **real OPA + Rego** policy evaluation against structured inputs (confidence, blast radius, deploy age, upstream health). Fail-closed: if OPA is unreachable, action is denied with reason `policy_engine_unavailable`. Live decisions at `demo/screenshots/opa/`. |
| "How do you know the fix actually worked?" | The verification loop re-checks the customer signal against the bundle's recovery band. `recovered=False` triggers `auto_revert_triggered` and demotes the verdict provenance to `observational`. The system will never claim recovery it cannot measure. Live proof: INC-4481 auto-reverts against a real bundle. |
| "How do you know the AI isn't overconfident?" | Posteriors hard-capped at 0.94, measured on a reliability diagram against real outcomes across 12 corpus bundles. LLM path never sources graded numbers. |
| "What happens if evidence is manipulated?" | Evidence is hashed, typed, and enters model context only as delimited data — never as an instruction. Dynamic-triage hypotheses have an additional anti-hallucination filter that drops any evidence ref not resolving to real prior evidence. Adjudicator has no tool access; only structured hypotheses reach the decision. Adversarial testing remains PLANNED (§6). |
| "Are customer-facing incidents treated differently?" | Yes — CIS routes urgency (P0 revenue path → CIS 100 critical; P3 internal → CIS 5.05 low). But CIS **never** touches the diagnostic path — enforced by static AST audit (0 imports from `ic.cis` across 10 diagnostic-path files) AND temporal ordering (CIS computed after verdict is frozen). |
| "Is this auditable after the fact?" | Yes — every verdict is Merkle-rooted over its evidence (including policy decisions and verification checks) and Ed25519-signed, independently verifiable offline with `verify.py`. |
| "Are you monitoring the AI's behavior over time in production?" | Not yet — production telemetry volume required doesn't exist here. The integration point is specified (`ROADMAP.md` stage 3, `RAI.md` §5); nothing is connected. Said directly, not implied. |

---

## 8. Version History

**Sep 20 (this update):**
  * §2.1 — moved intervention/rollback rows from "human Approve click" to
    "OPA policy evaluation"; added Verification (L2), CIS (L4) rows.
  * §2.2 — verification loop marked as shipped L3→L2 promotion.
  * §4  — corpus size 8 → 12 updated.
  * §5  — Arize scope decision preserved with fresh corpus number.
  * §6  — items 1 and 3 marked shipped; adversarial testing surfaced as
    highest remaining item.
  * §7  — Q&A table rewritten around what actually shipped (OPA, verification,
    CIS guardrail, anti-hallucination).

**Sep 11 (initial draft):** original L0-L4 taxonomy, containment
description, Arize NOT BUILT rationale, initial roadmap.
