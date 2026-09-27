<div align="center">

# Incident Commander

**An autonomous SRE agent that decides when *not* to act.**

Bayesian belief updating for probe selection · OPA policy engine outside the agent · Merkle-signed evidence chain · offline-verifiable postmortems.

[Quickstart](#quickstart) · [What it does](#what-it-does) · [How the mechanism works](#how-the-mechanism-works) · [Architecture](#architecture) · [Roadmap](#roadmap)

</div>

---

## What it does

Incident Commander is a decision layer above your observability stack. It watches telemetry from a target service, opens an incident autonomously when SLOs are breached, and runs a formal loop to decide what to do next:

1. **Investigate** — competing root-cause hypotheses, scored by expected information gain per unit cost
2. **Ask** — a policy engine outside the agent decides whether the proposed action is allowed
3. **Act** — only after policy approval, over real HTTP against the target service
4. **Verify** — from fresh telemetry collected *after* the action, not the pre-action signal
5. **Auto-revert** — if verification fails, the agent undoes its own change
6. **Seal** — every decision is a leaf in a Merkle tree, signed with Ed25519, offline-verifiable

The condition for autonomy is **reversibility, not confidence**. Irreversible actions never run.

## Why

Most AI SRE tools show an agent fixing something. This project shows an agent **refusing to act** when the proposed action is unsafe — and proving it was right to refuse.

Enterprise DevOps teams cannot deploy autonomous incident response until three things are true simultaneously:
- The agent can act on the same interfaces production actually exposes (HTTP + OpenTelemetry)
- Every action is gated by a policy the agent cannot bypass
- Every decision produces a receipt a third party can verify without trusting the operator

Incident Commander shows all three end-to-end.

## Quickstart

**Requirements:** Docker Desktop, Python 3.11+, Node 18+.

```bash
git clone https://github.com/S2800-0/incident-commander.git
cd incident-commander
pip install -e .
```

Then double-click `start.command` from Finder (macOS), or run:

```bash
./start.command
```

This launches, each in its own Terminal tab:

- OPA policy engine (Docker container, port 8181)
- Backend orchestrator + SLO detector (FastAPI on port 8000)
- Target service — the mock `shop-svc` (FastAPI on port 9001)
- Console (React + Vite on port 5173)

The console opens automatically in your browser. On the **Live** tab, click **▶ Start traffic (60 rps)**, then click any fault-injection scenario to watch the pipeline fire.

To stop everything cleanly: `./stop.command`.

## Try it

Once the console is open, four chaos scenarios exercise the four outcome patterns:

| Scenario | Expected outcome | What it proves |
|---|---|---|
| **Bad code release** | Autonomous rollback → verified recovery | Reversible actions execute end-to-end |
| **Config release + hidden dependency** | Rollback → verification fails → auto-revert → escalate | The system undoes its own action when the fix doesn't hold |
| **Two dependencies degrade together** | Abstain → escalate | Refusing when evidence cannot separate causes is a designed output |
| **orders-db saturation** | Failover proposed → **OPA DENY** → escalate | Irreversible actions are denied before they can execute |

Each scenario completes in 15-30 seconds. Between runs, click **⟲ Reset environment**.

## How the mechanism works

### Bayesian belief updating

Each hypothesis about the root cause carries a probability. When a probe returns evidence, the system compares the observation against what each hypothesis predicted:

- Predictions that matched → posterior probability **up**
- Predictions that mismatched → hypothesis **eliminated**

There is no learned model — the policy is deterministic Bayesian updating over structured hypothesis predictions. Same input, same output. Anyone can audit the logic by reading the code.

### Value-of-Information probe selection

Not every check is worth running. The system scores each candidate probe by expected information gain divided by cost, and picks the argmax over probes that haven't run yet. The stop rule is: continue while uncertainty remains **and** a discriminating check still exists.

```
next_check = argmax( expected_information_gain / cost )
```

Zero LLM tokens in the decision path.

### Policy-as-code outside the agent

Every state-changing action is gated by an [OPA](https://www.openpolicyagent.org/) engine running in its own Docker container, on its own port, with policies written in Rego. The backend calls OPA over HTTP with a policy input document. The agent has no code path to write or bypass these rules; if OPA is unreachable, the backend fails closed.

Three action classes exist: rollback, canary, and intervention. Each is gated by different envelope constraints (blast radius, reversibility, traffic percentage, duration).

### Merkle + Ed25519 evidence chain

Every decision — SLO breach, probe result, policy verdict, action outcome, verification result — is a leaf in a Merkle tree. The root is signed with Ed25519 over `(verdict || root || timestamp)`. A standalone verifier reads only the sealed postmortem file:

```bash
python verify.py results/LIVE-0073.postmortem.json
# → VERIFIED · exit 0
```

Tamper with any leaf, the same script prints `TAMPERED · exit 1`.

## Architecture

```
Live service (mock shop-svc)
        │  OpenTelemetry
        ▼
SLO detector  ──▶  Investigation loop  ──▶  Policy engine (OPA, outside agent)
                          │                          │
                          │                       ALLOW / DENY
                          │                          │
                          ▼                          ▼
                   Sealed evidence  ◀── Execute action (real HTTP)
                          │                          │
                          │                          ▼
                          │                    Verification
                          │                          │
                          ▼                          ▼
                    Merkle + Ed25519 audit chain (verify.py)
```

Ten engine modules · 15 unit tests · 14 policy tests · 63 end-to-end trials across five outcome patterns.

### Repository layout

```
ic/                 orchestrator, policy client, evidence chain, verify.py
├── live/           SLO detector, live controller, verification, probes
└── live_trials.py  63-trial harness across five outcome patterns

mock/               target service (shop-svc) + load generator
server/             FastAPI backend (orchestrator + REST + WebSocket)
console/            React + Vite console (LIVE tab + Console tab)
deck/               presentation deck (static HTML, self-contained)
policy/             Rego policies

corpus/             12 recorded incident bundles (replay mode)
tests/              pytest suites for orchestrator + policy + live loop

start.command       one-click startup (macOS Finder-double-click)
stop.command        one-click shutdown
```

## Evidence

Two levels of proof exist beyond a single demo run:

**Ablation** — turn the mechanism off and measure the collapse:
```
                            ON     OFF
Accuracy on hardest cases  100%    0%
False-positive rollbacks     0%   33%
Brier score               0.004  0.287
```

`OFF` scores 0% (not 50%) because the discriminating signal is reachable only through a check. Turn checks off and it is genuinely gone — the ablation measures the policy, not the model.

**63-trial harness** — five outcome patterns, all end-to-end through the same code:

```bash
python -m ic.live_trials
```

Results:
- Confirmed → acted → verified: **25 / 25**
- Won by exclusion → verification caught → reverted: **10 / 10**
- Reversible non-rollback action proposed: **9 / 9**
- Evidence insufficient → abstained: **5 / 5**
- Irreversible action proposed → policy denied: **14 / 14**

**Ours vs. exhaustive baseline (same 25 confirmed trials):**
| | Ours | Exhaustive |
|---|---|---|
| Resolution rate | 100% | 100% |
| Probes per incident | 1.2 | 2.8 |
| Harmful actions | 0 | 0 |
| Unreverted changes | 0 | 0 |

Same accuracy at 43% of the probing cost.

## Stack

- **Python 3.11+ · FastAPI · uvicorn · httpx** — backend and target service
- **Open Policy Agent + Rego** — external policy engine (Docker sidecar)
- **OpenTelemetry** — telemetry ingestion format
- **React · Vite · TypeScript** — console
- **cryptography** — Ed25519 signatures
- **pytest** — unit and integration tests

No LLM in the decision path. LLM support exists as an opt-in triage assist behind a two-check anti-hallucination filter (`IC_USE_LLM=1`), but the metrics above are produced without it.

## Roadmap

**Shipped:**
- Investigation loop with Value-of-Information probe selection
- OPA + Rego policy engine, running outside the agent
- Verification loop with recovery band and auto-revert
- Customer Impact Scoring (P0–P3)
- OpenTelemetry ingestion + SLO breach detector (with hysteresis)
- 63-trial end-to-end harness across five outcome patterns
- Real HTTP execution against a live target service
- Merkle + Ed25519 audit chain, offline verifier
- Probe catalog expansion — VoI 1.2 vs exhaustive 2.8 probes per incident

**Planned:**
- Formal Expected Information Gain under Shannon entropy (currently a divergence heuristic)
- Kubernetes adapter — replace `mock/shop_svc.py` with a real `kubectl` integration
- Wider policy corpus — compliance, data residency, multi-tenant scoping
- Multi-region deployment with per-region SLO detectors
- Datadog / Grafana / Prometheus telemetry adapters

## Documentation

- [`docs/SRS.md`](docs/SRS.md) — Software Requirements Specification with traceability
- [`docs/RAI.md`](docs/RAI.md) — Responsible AI governance and safety
- [`docs/ROI.md`](docs/ROI.md) — Business impact and value tiers
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — Detailed roadmap
- [`docs/SUSTAINABILITY.md`](docs/SUSTAINABILITY.md) — Waste-reduction metrics

## Contributing

Issues and pull requests welcome. The project follows conventional commits and standard pytest discipline. Run `pytest` before opening a PR.

## License

MIT — see `LICENSE`.

## Authors

Shahesta Salama · Nourseen Tarek · Alryada University
