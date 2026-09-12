# ROI / Business Case — Incident Commander

**Version:** 1.0 (Sep 20 — refreshed on wider corpus)
**Status:** Model DONE (implemented as `ic/roi.py`); INPUT assumptions still labeled as placeholders.
**Companion to:** [`SRS.md`](SRS.md) FR-6.1–6.3, [`ROADMAP.md`](ROADMAP.md).

---

## 0. How to use this document

Every number below is either:
- **(DATA)** — pulled directly from the corpus or the ablation harness, real
  and reproducible via `python -m ic.harness`, or
- **(INPUT)** — a placeholder assumption you must replace with your own or a
  target organization's real figures before presenting this as a business case.

**Do not present the (INPUT) numbers as real to the judges.** Present the
*model* — the formula and the real (DATA) inputs — and say plainly that the
dollar figure depends on assumptions the judges can substitute their own
numbers into. That is a stronger position than a fabricated total, and it's
consistent with every other honesty disclosure in this project's documentation.

---

## 1. The one sentence

> Guided investigation removes both kinds of error a human under time pressure
> makes: reaching a wrong or unresolved diagnosis (100% → 0% on the corpus's
> hardest cases with probes off), and acting on a wrong diagnosis by rolling
> back an innocent deploy (38% → 0% false-positive rollback rate).

Both of those failure modes have a $ cost. This document quantifies them.

---

## 2. Real inputs (DATA — from this repo, reproducible)

Refreshed Sep 19 on the wider 12-bundle corpus. `python -m ic.roi` reads
these values from `harness_results.json` at runtime; the model updates
automatically whenever the corpus grows.

| Metric | Value | Source |
|---|---|---|
| Top-1 accuracy, hard slices, probes OFF | 0% (0/6) | `python -m ic.harness` |
| Top-1 accuracy, hard slices, probes ON | 100% (6/6) | `python -m ic.harness` |
| False-positive rollback rate, OFF | 33% (4/12 bundles) | `python -m ic.harness` |
| False-positive rollback rate, ON | 0% (0/12 bundles) | `python -m ic.harness` |
| Brier score (calibration), OFF → ON | 0.287 → 0.004 | `python -m ic.harness` |
| Average human diagnostic-time baseline, across 12 authored bundles | **2,442 sec (≈ 40.7 min)** | `human_baseline_seconds` field, averaged across `corpus/*.json` |
| Hard-slice fraction of corpus (ambiguous + adversarial_redherring) | 50% (6/12) | `corpus/` |
| Corpus size | 12 bundles, 5 slices | `corpus/` |

**What `human_baseline_seconds` actually represents:** a per-bundle estimate,
authored alongside the ground truth, of how long a human engineer takes to
reach the *correct* root-cause hypothesis for that scenario. It is a diagnostic-
time figure, not a full incident-lifecycle (detect→mitigate→resolve) figure —
treat it as the portion of MTTR this system's mechanism actually targets.

---

## 3. A number we are explicitly NOT using, and why

The harness also records `time_to_conclusion_s` — the wall-clock time the
*deterministic engine* takes to reach a verdict. Across the corpus this is
approximately **0.001–0.005 seconds**.

**We are not building an ROI claim on "the system is 500,000x faster than a
human."** That comparison is not meaningful: the prototype reasons over
fixture data already loaded in memory, not live telemetry with real query
latency (network round-trips, dashboard render time, log search indexing).
A fair time comparison requires a live-connector deployment, which is a
roadmap item (see `SRS.md` §3), not something this prototype can honestly
claim yet.

If a judge asks "so it's instant?" — the answer is: *"The decision logic runs
in milliseconds; a production deployment's wall-clock time would be dominated
by the latency of the telemetry queries themselves, which we haven't measured
because we don't have a live connector yet. We're not claiming a speed number
we can't defend."*

---

## 4. The ROI model

Two value drivers, kept separate because they have different confidence
levels.

### 4.1 Driver A — Avoided wrong-diagnosis cost (higher confidence)

An unresolved or wrong diagnosis doesn't just cost the original investigation
time — it costs a *second* investigation cycle once the first conclusion is
found to be wrong, plus the customer-facing time the incident stayed
unresolved.

```
Formula:
  Avoided_Cost_A = N_incidents_per_month
                  × P_hard_slice_incident          (INPUT — your fraction of
                                                      incidents that are
                                                      genuinely ambiguous /
                                                      adversarial, not
                                                      first-glance-obvious)
                  × Δ_accuracy                       (DATA: 1.00, i.e. 0%→100%)
                  × Avg_diagnostic_time_baseline     (DATA: 2,442 sec)
                  × Cost_per_engineer_second         (INPUT)
```

**Worked example (placeholder inputs — replace before presenting):**

| Variable | Placeholder value | Type |
|---|---|---|
| Incidents per month | 40 | INPUT |
| Fraction that are hard-slice-like (ambiguous/adversarial) | 50% (from corpus, override with real org number) | DATA/INPUT |
| Δ accuracy on those | 100% (1.00) | DATA |
| Avg diagnostic time baseline | 2,442 sec | DATA |
| Fully-loaded engineer cost per hour | $75 | INPUT |
| Cost per second | $0.0208 | derived |

```
Avoided_Cost_A = 40 × 0.50 × 1.00 × 2,442 × 0.0208
              ≈ $1,017 / month in avoided re-investigation time
```

**Note on the 50%:** this is the fraction of *the corpus* that is hard-slice
— we designed it that way to stress-test the mechanism, not because 50% of a
real org's incidents are ambiguous. `python -m ic.roi --hard-slice-pct 0.25`
substitutes any real-world estimate; the formula updates automatically.

This is a **lower bound** — it only counts the diagnostic-time portion, not
the extended customer-facing downtime a wrong or delayed diagnosis causes.

### 4.2 Driver B — Avoided false-positive rollback cost (medium confidence)

A rollback that doesn't fix the problem costs: the rollback execution itself,
the time to notice it didn't help, and the delay before the real cause is
found — plus, in the worst case, the rollback introduces its *own* regression.

```
Formula:
  Avoided_Cost_B = N_incidents_per_month
                  × Δ_FP_rollback_rate               (DATA: 0.33 → 0, Δ=0.33)
                  × Cost_per_bad_rollback             (INPUT)
```

**Worked example (placeholder inputs):**

| Variable | Placeholder value | Type |
|---|---|---|
| Incidents per month | 40 | INPUT |
| Δ FP rollback rate | 33% (0.33) | DATA |
| Cost per bad rollback (wasted effort + re-investigation + risk of a rollback-induced regression) | $600 | INPUT |

```
Avoided_Cost_B = 40 × 0.333... × 600 = $8,000 / month
```

**Why this number is larger and less certain than Driver A:** it depends
heavily on `Cost_per_bad_rollback`, which varies enormously by organization —
a rollback on a low-traffic internal service costs little; a rollback that
itself causes a customer-facing regression on a revenue path can cost far
more than $600. Present this range explicitly rather than the point estimate
alone if asked to defend it.

### 4.3 Combined, illustrative total

```
Total_Monthly_Value ≈ Avoided_Cost_A + Avoided_Cost_B
                     ≈ $1,017 + $8,000
                     ≈ $9,017 / month  (illustrative — see caveats)
                     ≈ $108,204 / year
```

The `python -m ic.roi` script always computes and prints these numbers
fresh from the current `harness_results.json` — if the corpus grows again,
the totals move; the model is the deliverable, not the specific dollar
figure.

**Say this out loud, not just the number:** *"This is a model with two real,
reproducible inputs — the accuracy delta and the false-positive-rollback
delta — and placeholder assumptions for incident volume and cost-per-incident
that we'd replace with a real organization's numbers. The formula is the
deliverable; the dollar figure is illustrative until someone plugs in their
own operational data."*

---

## 5. Sensitivity — what moves this number most

In order of leverage:

1. **`Cost_per_bad_rollback`** — the single highest-leverage, least-certain
   input. A 2x change here roughly doubles Driver B.
2. **Incident volume** (`N_incidents_per_month`) — linear scaling on both
   drivers.
3. **Fraction of incidents that are "hard-slice-like"** — this is the input
   most in need of real validation; it's currently a guess, not measured
   against any real incident population. Growing the corpus from real
   postmortems (SRS.md roadmap) is the path to actually measuring this
   fraction instead of assuming it.

---

## 6. Caveats (read before presenting this slide)

- **12-bundle corpus.** The accuracy and rollback deltas are real and
  reproducible, but they come from a small, team-authored corpus — see
  `SRS.md` §2.4 and the existing README disclosure. This model should be
  framed as "what the mechanism is worth *if* the ablation generalizes,"
  not as a validated production result.
- **No live telemetry connector.** Every number here assumes the mechanism
  works the same way against real Prometheus/Datadog queries as it does
  against fixture JSON. That is the next validation step (SRS.md, external
  interfaces), not something already proven.
- **`human_baseline_seconds` is author-estimated**, not measured against real
  incident response logs. It is a reasonable proxy (used consistently across
  all 12 bundles), but it is an estimate, and should be named as such if asked.
- **This document computes a monthly recurring value estimate, not a
  build/deployment cost.** A complete ROI case also needs the cost side —
  engineering time to build the live connector, ongoing LLM API costs (if the
  live-LLM path is used in production), and operational overhead. That cost
  side is not modeled here and should be added before this is used as an
  actual investment decision document.

---

## 7. One-line answer for "what's the ROI?"

> "Two real, measured deltas — 100% accuracy recovery on the hardest incident
> types, and elimination of false-positive rollbacks — translated into a
> dollar model with transparent assumptions. Plug in your incident volume and
> cost-per-incident, and the formula gives you a defensible number. We're not
> claiming a fabricated total; we're showing the mechanism that produces one."
