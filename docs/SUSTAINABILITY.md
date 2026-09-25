# Sustainability — Incident Commander

**Version:** 1.0 (Sep 2026, hackathon submission)
**Companion to:** [`ROI.md`](ROI.md), [`RAI.md`](RAI.md), [`ROADMAP.md`](ROADMAP.md)
**Audience:** hackathon judges, engineering leadership, reviewers evaluating
sustainability in the agentic-AI-era DevOps setting.

---

## 0. What this document is (and isn't)

This is **not a green-metrics claim**. We do not produce CO₂ estimates. We
do not compare energy footprints against named vendors. We do not benchmark
kWh/incident.

This IS a structural argument: **an autonomous DevOps system that runs only
the operational actions the evidence says are worth running is, by
construction, more sustainable than one that runs autonomous actions
freely.** Every unnecessary probe against production observability is
compute. Every false-positive rollback is a full deploy-pipeline
execution. Every second in a degraded state amplifies user retry traffic.
Reducing any of those is compute — and human time — that did not have to
be spent.

The mechanisms we shipped for governance and correctness reasons happen to
carry sustainability properties as side-effects. This document names them
honestly.

---

## 1. The three axes of waste our architecture avoids

### 1.1 Investigation cycles (probes, queries, log scans)

**Traditional pattern.** Under alert fatigue, an unassisted responder (or a
naive AI agent) runs "check everything" — every probe in the catalogue,
every dashboard, every log query, until something stands out.

**Our mechanism.** VoI + Expected Information Gain: at each step, pick the
one probe with highest `info_gain − λ·cost`. The `cost` term is literal —
`Probe.cost_ms` in `ic/models.py`. High-cost probes are only run when
their expected information gain justifies the cost.

**Measured today** (12-bundle corpus, from `python -m ic.harness`):

| | Total | Per incident |
|---|---|---|
| Probe catalogue (would run in "check everything") | 24 | 2.0 |
| Probes actually executed by VoI | 15 | 1.3 |
| **Probes avoided** | **9 (38%)** | **0.75** |

Every avoided probe is a query that did not hit the observability stack.
Prometheus range queries, ELK searches, distributed-trace lookups — none
of them free. At 40 incidents/month in a production org, the same 38%
avoidance rate is ~30 probe queries not incurred per month per incident
class.

### 1.2 Deploy-pipeline executions (false-positive rollbacks)

**Traditional pattern.** Ambiguous incident → oncall rolls back the recent
deploy → the rollback doesn't fix it (because the deploy wasn't the
cause) → the deploy is re-rolled-forward later. Each rollback is a full CI
run: image pulls, k8s reschedules, load-balancer rebalance, cache warmup.

**Our mechanism.** Probes-off arm shows 33% FP-rollback rate on the
12-bundle corpus. Probes-on arm shows 0%. Every one of those avoided
rollbacks is a full deploy pipeline that did not have to run.

**Measured today:**

| | Total across corpus |
|---|---|
| FP rollbacks with probes OFF | 4 |
| FP rollbacks with probes ON | 0 |
| **Pipelines avoided** | **4** |

At 40 incidents/month, extrapolating a similar delta = ~13 avoided
pipeline runs per month. Each of those is real CI compute, image
registry bandwidth, container startup, cluster reschedule pressure.

### 1.3 Wasted degraded-state minutes (via verification + auto-revert)

**Traditional pattern.** An intervention (canary, traffic shift) runs its
full time budget even when it didn't help — because there's no automatic
check that catches "the fix didn't work." User traffic during that window
retries repeatedly, amplifying load, cascading to dependent services.

**Our mechanism.** Verification loop re-checks the customer signal
against a recovery band immediately after intervention. `recovered=False`
fires `auto_revert_triggered` at the *earliest* signal that the action
didn't work — not at the end of a 60-second timer.

**Design property, not measured yet** — we don't have a live production
deployment to measure how many degraded-state minutes are actually
saved. But the mechanism is structurally in place; see
`ic/verification.py` and INC-4481 in the corpus for the failure-path
demonstration.

---

## 2. Design decisions that carry a sustainability property

Every one of these was made for a governance / correctness reason. The
sustainability angle is a side-effect worth naming.

| Decision | Where it lives | Sustainability property |
|---|---|---|
| `VoI(a) = EIG − λ·cost − μ·risk` — cost-aware action scoring | [`ic/voi.py`](../ic/voi.py) | Every action's compute cost is a first-class term in the selection, not an afterthought |
| Bounded probe loop (K=4, T=45s, τ=0.15) | [`ic/orchestrator.py`](../ic/orchestrator.py) | Hard cap on investigation compute per incident — no unbounded exploration |
| OPA policy gate — DENYs a wasted deploy that doesn't meet evidence sufficiency | [`policy/incident/action.rego`](../policy/incident/action.rego) | Every DENY is a pipeline run avoided; every ALLOW is one the evidence justifies |
| Bounded intervention envelope (5% traffic, 60s duration, auto-revert) | [`ic/voi.py`](../ic/voi.py), OPA rule | Blast radius is bounded — canary compute cost is capped in advance |
| Verification loop with auto-revert | [`ic/verification.py`](../ic/verification.py) | Failed action reverts at the first signal, not at the timer end |
| Opt-in LLM (`IC_USE_LLM=1`) — harness runs deterministic | [`ic/llm.py`](../ic/llm.py), [`ic/harness.py`](../ic/harness.py) | The graded artifact consumes zero LLM tokens. Inference is spent only on the live-demo novel-scenario path |
| Anti-hallucination filter on dynamic hypotheses | [`ic/dynamic_triage.py`](../ic/dynamic_triage.py) | Invented evidence refs are dropped before they can cause the reasoner to run wasted follow-up probes |
| Sealed evidence chain — one authoritative audit trail | [`ic/evidence_chain.py`](../ic/evidence_chain.py) | Reduces the "let me investigate what happened last week" cycle — postmortem is already sealed |

---

## 3. What we explicitly do NOT claim

  * **No CO₂ or kWh conversion.** We do not have credible per-query energy
    data for the observability stack we'd hit. Any conversion would be a
    guess.
  * **No comparative vendor benchmark.** We don't run against Datadog's
    Bits AI or Grafana's LLM assistant. Our baseline is the deterministic
    ablation-off arm — an intentional stand-in for unassisted
    investigation, not a competitor benchmark. `docs/SRS.md` §5 NFR-3 and
    the "limitations" slide 8 already say so.
  * **No sustainability KPI as a first-order goal.** Making the system
    green was not why we built the mechanisms. Not falling into the
    industry failure modes was. Sustainability is a by-product, and we
    frame it that way.

---

## 4. Additional industry failure modes our architecture addresses

The user-supplied positioning document ("Industry Problems.pdf") names
three failure modes: **AI-driven change velocity**, **signal-to-noise /
alert fatigue**, and **unsafe autonomous decisions**. Beyond those three,
the DevOps / AI-SRE literature documents several more, each of which our
shipped mechanisms happen to address. Named here for completeness.

| Failure mode | Where documented | Our mechanism |
|---|---|---|
| **Auto-remediation cascades / retry storms** — an autonomous fix amplifies an incident | Google SRE book §22 (Handling Overload); Meta postmortems | Verification loop + `auto_revert_triggered` — a failed action reverts before it can cascade. Bounded envelope (5% traffic) caps blast. |
| **Confidence miscalibration** — LLM/agent reports high confidence on a wrong answer | arXiv AI-SRE literature; LLM-uncertainty papers | Posterior hard-capped at 0.94 (`ic/reasoner.py`); Brier score measured against real outcomes; reliability diagram in the harness. |
| **Prompt injection / evidence poisoning** — attacker crafts a log line that redirects an agent | OWASP LLM Top-10; RAI containment literature | Adjudicator has no tool access; evidence enters as delimited/typed/hashed data; anti-hallucination filter drops invented refs. Full argument in `RAI.md` §3. |
| **Explainability gap** — action fired, no one can explain why | EU AI Act §13; ISO/IEC 42001 auditability | Merkle-rooted + Ed25519-signed evidence chain over every cited leaf, offline-verifiable via `verify.py`. Every decision is reconstructable from the sealed audit trail. |
| **Blast-radius uncertainty** — agent doesn't know how far its action reaches | Argo Rollouts docs; Netflix Chaos Engineering | Safety envelope hard-coded per action class (max_traffic_pct, max_duration_s, auto_revert). OPA rule enforces envelope compliance before execution. |
| **Compound automation risk** — multiple autonomous systems reacting to each other | LinkedIn IAT paper; Datadog Bits AI postmortem lessons | Roadmap: staged autonomy ladder (`RAI.md` §2.2) — L3→L2 promotions only for provably safe classes; shadow mode before selective NoOps (`ROADMAP.md` stage 9). |
| **Benchmark contamination** — accuracy measured on the same data the system was tuned on | ML reproducibility literature | Slice-based headline (hard slices only); `ROADMAP.md` stage 8 (real postmortem corpus) is the honest next step. Named as a limitation on deck slide 8. |
| **Feedback-loop breakdown** — agent acts but gets no signal back, cannot verify | AI safety literature on closed-loop control | Verification loop is exactly this: post-action signal check, sealed into the audit chain regardless of outcome. |

**Framing note.** Same as the PDF's framing: we do not claim we invented
solutions to these problems. We claim our decision architecture doesn't
fall into these failure modes — and each mechanism is a running module
with a screenshot, not an aspiration.

---

## 5. What the deck / video should say (and what it shouldn't)

**Say:**

> "The mechanisms we shipped for governance were also, structurally, a
> compute-efficiency filter. VoI probe selection reduces investigation
> queries by 38% on our corpus. The policy gate blocks deploy pipelines
> that wouldn't have helped. The verification loop reverts failed
> actions immediately rather than at timer end. Every one of those is a
> resource the system did not consume. That's sustainable agentic DevOps
> — not a green claim, a discipline."

**Don't say:**

  * "X% less carbon than the competition." (No baseline, no measurement.)
  * "N kWh saved per incident." (No credible conversion.)
  * "Our AI is sustainable." (Too vague; the architecture is what carries
    the property.)

---

## 6. Roadmap — sustainability items not yet built

Ordered by "which one earns the most credibility if built next":

  1. **Live-telemetry query-cost accounting** — once ROADMAP stage 1
     (OpenTelemetry live ingestion) is wired, every probe execution can
     record the actual query cost against the observability backend
     (Prometheus range-scan bytes, Loki lines scanned, Tempo trace
     lookups). Replaces the "cost_ms as a proxy" model with real data.
  2. **Wasted-degraded-minutes counter** — the verification loop's
     early-revert saves time; measuring how much requires real
     production-incident traces we don't have yet.
  3. **Corpus growth from real postmortems** (ROADMAP stage 8) — makes the
     "avoided FP rollbacks" number generalise to real distributions
     instead of the benchmark distribution.
  4. **Comparative benchmark** — running the same corpus through an
     autonomous baseline (a "check everything" agent) and reporting the
     probe-count and pipeline-count delta directly. Not built yet because
     the honest baseline requires implementing that agent, and that's
     scope for a separate submission.

---

## 7. One-line answer for "how does this connect to sustainability?"

> *"Sustainable agentic DevOps is the discipline of only running the
> operational actions the evidence says are worth running. Every
> unnecessary probe is compute. Every false-positive rollback is a
> pipeline. Every wasted degraded-state minute is amplified user retry
> traffic. Our decision architecture — VoI probe selection, OPA policy
> gate, verification-driven auto-revert — is a governance filter first
> and a compute-efficiency filter as a side-effect. `python -m ic.harness`
> prints the current numbers: **9 probes avoided (38% of catalog), 4
> pipeline runs not triggered, 0 LLM tokens burned for graded output**."*
