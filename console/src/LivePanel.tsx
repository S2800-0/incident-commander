import { useEffect, useMemo, useRef, useState } from "react";

/* LIVE MODE — shows what the autonomous controller is doing. Nothing on this
   screen starts, steers or approves an investigation: incidents open from
   telemetry and OPA decides every action. The only controls are the
   environment's fault injection (clearly separated) and choosing which
   incident to look at. */

type LiveEvent = { seq: number; ts: number; type: string; incident_id: string | null; [k: string]: any };

const SCENARIOS: { id: string; label: string; expect: string }[] = [
  { id: "bad_deploy", label: "Bad code release", expect: "expected: diagnose → rollback → verified RESOLVED" },
  { id: "config_regression_hidden_dependency", label: "Config release + hidden dependency fault",
    expect: "expected: rollback → verification fails → auto-revert → escalate" },
  { id: "correlated_dependency_degradation", label: "Two dependencies degrade together",
    expect: "expected: cannot separate causes → abstain → escalate" },
  { id: "database_saturation", label: "orders-db saturation",
    expect: "expected: diagnose DB → failover proposed → OPA DENY → escalate" },
];

const pct = (x?: number | null, d = 1) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`);
const ms = (x?: number | null) => (x === null || x === undefined ? "—" : `${Math.round(x)} ms`);
const clock = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");

function statusTone(s?: string): string {
  if (!s) return "pill-neutral";
  if (s === "RESOLVED") return "pill-healthy";
  if (s === "INVESTIGATING") return "pill-info";
  if (s === "ERROR") return "pill-critical";
  return "pill-major";
}

function statusLabel(s?: string): string {
  return ({
    RESOLVED: "Resolved autonomously",
    ESCALATED_INSUFFICIENT_EVIDENCE: "Escalated · insufficient evidence",
    ESCALATED_POLICY_DENIED: "Escalated · policy denied action",
    ESCALATED_REMEDIATION_FAILED: "Escalated · remediation failed, reverted",
    ESCALATED_POLICY_ENGINE_UNAVAILABLE: "Escalated · policy engine down (fail-closed)",
    ESCALATED_ACTION_FAILED: "Escalated · action call failed",
    INVESTIGATING: "Investigating",
    ERROR: "Error",
  } as Record<string, string>)[s || ""] || s || "—";
}

function describe(e: LiveEvent): string {
  switch (e.type) {
    case "incident_detected": return `SLO breach detected: ${e.alert?.condition}`;
    case "evidence_collected": return `collected ${e.source_uri}${e.points ? ` (${e.points} points)` : ""}`;
    case "hypothesis_generated": return `hypothesis ${e.id}: ${e.claim}`;
    case "agent_reasoned": return `${e.agent_id} → ${e.hyp_id} ${e.delta >= 0 ? "+" : ""}${e.delta}: ${e.rationale}`;
    case "beliefs_updated": return `beliefs updated from ${e.source} · entropy ${e.entropy_nats} nats`;
    case "probe_selected": return `selected ${e.probe_id} (EIG ${e.eig_nats} nats, ${e.cost_ms} ms) — ${e.reason}`;
    case "probe_executed": return e.blocked_by_policy ? `${e.probe_id} blocked by policy`
      : `${e.probe_id} → ${e.outcome ?? "inconclusive"} · belief change ${e.realized_gain_nats} nats${e.wasted ? " · WASTED" : ""}`;
    case "hypothesis_ruled_out": return `${e.id} ruled out: ${e.reason}`;
    case "diagnosis": return `diagnosis ${e.root_cause_id} at ${pct(e.posterior)} (${e.confirmation})`;
    case "abstained": return `abstained: ${e.reason}`;
    case "remediation_proposed": return `proposed ${e.action} (${e.reversible ? "reversible" : "IRREVERSIBLE"}, blast radius ${e.blast_radius})`;
    case "policy_decision": return `OPA ${e.allow ? "ALLOW" : "DENY"} ${e.action}${e.reasons?.length ? ` — ${e.reasons.join(", ")}` : ""}`;
    case "action_started": return `executing ${e.action}: ${e.detail}`;
    case "action_blocked": return `blocked ${e.action}: ${(e.reasons || []).join(", ")}`;
    case "remediation_executed": return `${e.action} executed on the service: ${e.version_before} → ${e.version_after}`;
    case "verification_started": return `verifying from fresh telemetry (${e.window})`;
    case "verification_result": return `verification: ${e.recovered ? "RECOVERED" : "NOT RECOVERED"} — ${pct(e.after?.error_rate)} errors over ${e.after?.requests} requests`;
    case "auto_revert_triggered": return `auto-revert triggered: ${e.reason}`;
    case "auto_revert_executed": return `auto-revert executed: service back on ${e.version_after}`;
    case "escalated": return `escalated: ${e.reason}`;
    case "customer_impact_computed": return `customer impact: ${e.failed_requests} failed requests of ${e.requests}`;
    case "chain_sealed": return `evidence chain sealed: ${e.leaves} leaves · verified ${e.verified ? "✓" : "✗"}`;
    case "incident_closed": return `closed: ${statusLabel(e.status)} in ${e.time_to_close_s}s`;
    case "budget_exhausted": return `investigation budget exhausted after ${e.probes} probes`;
    case "incident_error": return `error: ${e.error}`;
    default: return e.type;
  }
}

const QUIET = new Set(["agent_started", "probes_scored", "mode_changed", "evidence_collected"]);

export default function LivePanel() {
  const [state, setState] = useState<any>(null);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [env, setEnv] = useState<any>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [showEnv, setShowEnv] = useState(true);
  const [resetBusy, setResetBusy] = useState(false);
  const [loadgen, setLoadgen] = useState<{ running: boolean; rps: number } | null>(null);
  const [loadgenBusy, setLoadgenBusy] = useState(false);
  const [envBusy, setEnvBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const lastSeq = useRef(0);

  useEffect(() => {
    let alive = true;
    const pullState = async () => {
      try {
        const r = await fetch("/live/state");
        if (!r.ok) throw new Error(`live state ${r.status}`);
        const s = await r.json();
        if (alive) { setState(s); setErr(null); }
      } catch (e: any) { if (alive) setErr("Incident Commander server unreachable on :8000"); }
    };
    const pullEvents = async () => {
      try {
        const r = await fetch(`/live/events?since=${lastSeq.current}&limit=1000`);
        const d = await r.json();
        if (alive && d.events.length) {
          lastSeq.current = d.last_seq;
          setEvents((prev) => [...prev, ...d.events].slice(-6000));
        }
      } catch { /* shown via state error */ }
    };
    const pullEnv = async () => {
      try { const r = await fetch("/api/chaos/state"); if (alive && r.ok) setEnv(await r.json()); } catch { if (alive) setEnv(null); }
    };
    const pullLoadgen = async () => {
      try { const r = await fetch("/api/loadgen/state"); if (alive && r.ok) setLoadgen(await r.json()); } catch { if (alive) setLoadgen(null); }
    };
    pullState(); pullEvents(); pullEnv(); pullLoadgen();
    const a = setInterval(pullState, 1000), b = setInterval(pullEvents, 600),
          c = setInterval(pullEnv, 2500), d = setInterval(pullLoadgen, 2500);
    return () => { alive = false; clearInterval(a); clearInterval(b); clearInterval(c); clearInterval(d); };
  }, []);

  const active = state?.active_incident?.incident_id as string | undefined;
  const shownId = active || focus || state?.incidents?.[0]?.incident_id || null;
  const ev = useMemo(() => events.filter((e) => e.incident_id === shownId), [events, shownId]);
  const view = useMemo(() => deriveView(ev), [ev]);
  const record = state?.incidents?.find((r: any) => r.incident_id === shownId);
  const status: string | undefined = active === shownId ? "INVESTIGATING" : (view.closed?.status || record?.status);

  async function inject(path: string) {
    setEnvBusy(true);
    try { await fetch(`/api/chaos/${path}`, { method: "POST" }); setFocus(null); } finally { setEnvBusy(false); }
  }

  // Full reset — stops any active chaos scenario AND flips the target service
  // back to its healthy stable-release baseline. Use this between demo runs so
  // no residual scenario state contaminates the next one.
  async function resetToBaseline() {
    setResetBusy(true);
    try {
      await fetch("/api/chaos/clear", { method: "POST" });
      await fetch("/api/shop/reset", { method: "POST" });
      setFocus(null);
    } finally { setResetBusy(false); }
  }

  // Toggle the in-process load generator on shop-svc. Without traffic there is
  // nothing for the SLO detector to observe, so the pipeline never fires.
  async function toggleLoadgen() {
    setLoadgenBusy(true);
    try {
      const running = !!loadgen?.running;
      await fetch(running ? "/api/loadgen/stop" : "/api/loadgen/start?rps=60", { method: "POST" });
    } finally { setLoadgenBusy(false); }
  }

  const h = state?.service_health;
  return (
    <div className="lv">
      {/* ---- system strip ---- */}
      <section className="lv-strip">
        <span className="lv-live"><span className="lv-pulse" />LIVE</span>
        <span className="lv-kv"><b>Autonomy</b> no human approval in the normal path · OPA decides every action</span>
        <span className="lv-kv"><b>Detector</b> {state ? (state.investigating ? "investigating" : state.armed ? "armed" : "cooling down") : "—"}</span>
        <span className={`lv-kv ${state?.opa_available ? "" : "bad"}`}><b>OPA</b> {state?.opa_available === undefined ? "—" : state.opa_available ? "reachable" : "DOWN — fail-closed"}</span>
        <span className="lv-kv"><b>Telemetry</b> {state?.telemetry?.last_ingest_age_s ?? "—"}s ago</span>
        <span className="lv-kv"><b>Policy</b> {state?.mode === "eig" ? "EIG investigation" : `baseline: ${state?.mode ?? "—"}`}</span>
        <span className="lv-spacer" />
        <span className="lv-metric"><small>RPS</small>{h ? h.rps.toFixed(0) : "—"}</span>
        <span className={`lv-metric ${h?.error_rate > 0.05 ? "bad" : ""}`}><small>5xx</small>{pct(h?.error_rate)}</span>
        <span className="lv-metric"><small>P95</small>{ms(h?.p95_ms)}</span>
      </section>
      {err && <div className="lv-error">{err}</div>}

      {!shownId && (
        <section className="card lv-empty">
          <div className="body">
            <h3>Watching checkout-service</h3>
            <p>No incident yet. The detector opens one on its own when the 5xx rate exceeds {pct(state?.slo?.error_rate_max, 0)} or
              p95 exceeds {state?.slo?.p95_max_ms} ms for {state?.slo?.consecutive} consecutive seconds.
              To see it work, break the environment below.</p>
          </div>
        </section>
      )}

      {shownId && (
        <>
          {/* ---- incident header ---- */}
          <section className="card lv-head">
            <div className="body">
              <div className="lv-headRow">
                <span className="lv-id">{shownId}</span>
                <span className={`pill ${statusTone(status)}`}>{statusLabel(status)}</span>
                {view.detected && <span className="lv-cond">{view.detected.alert?.condition}</span>}
                <span className="lv-spacer" />
                <span className="lv-muted">detected {clock(view.detected?.ts)}{view.closed ? ` · closed in ${view.closed.time_to_close_s}s` : ""}</span>
              </div>
              <Stepper view={view} status={status} />
              <div className="lv-now"><b>{status === "INVESTIGATING" ? "Now:" : "Last step:"}</b> {view.now ? describe(view.now) : "—"}</div>
            </div>
          </section>

          <div className="lv-grid">
            {/* ---- hypotheses ---- */}
            <section className="card">
              <h2>Hypotheses · generated from live context</h2>
              <div className="body">
                {!view.hyps.length && <div className="idle">gathering evidence…</div>}
                {view.hyps.map((hy) => {
                  const p = view.posteriors[hy.id] ?? 0;
                  const lead = view.leader === hy.id;
                  return (
                    <div key={hy.id} className={`lv-hyp ${view.ruledOut[hy.id] ? "out" : ""} ${lead ? "lead" : ""}`}>
                      <div className="lv-hypTop">
                        <span className="lv-hypId">{hy.id}</span>
                        <span className="pill pill-neutral">{hy.kind}</span>
                        {view.confirmed.includes(hy.id) && <span className="pill pill-healthy">signature confirmed</span>}
                        {view.ruledOut[hy.id] && <span className="pill pill-neutral">ruled out</span>}
                        <span className="lv-spacer" />
                        <b className="lv-p">{pct(p)}</b>
                      </div>
                      <div className="lv-bar"><span style={{ width: `${Math.max(1, p * 100)}%` }} /></div>
                      <div className="lv-claim">{hy.claim}</div>
                      <div className="lv-preds">
                        {hy.predictions.map((pr: any) => (
                          <span key={pr.observable} className={`lv-pred ${pr.role}`}>{pr.observable} = {pr.prediction}</span>
                        ))}
                      </div>
                    </div>
                  );
                })}
                {view.entropy.length > 1 && (
                  <div className="lv-entropy">
                    uncertainty (nats): {view.entropy.map((x, i) => <span key={i}>{i ? " → " : ""}<b>{x.toFixed(2)}</b></span>)}
                  </div>
                )}
              </div>
            </section>

            {/* ---- investigation ---- */}
            <section className="card">
              <h2>Investigation · next check chosen by expected information gain</h2>
              <div className="body">
                {!view.scored && <div className="idle">no probes scored yet</div>}
                {view.scored && (
                  <table className="lv-table">
                    <thead><tr><th>candidate probe</th><th>EIG</th><th>cost</th><th>EIG / s</th></tr></thead>
                    <tbody>
                      {view.scored.candidates.map((c: any) => (
                        <tr key={c.probe_id} className={view.lastSelected?.probe_id === c.probe_id ? "sel" : ""}>
                          <td>{c.probe_id}{c.interventional && <span className="pill pill-major lv-inl">changes traffic</span>}</td>
                          <td>{c.eig_nats.toFixed(3)}</td><td>{c.cost_ms} ms</td><td>{c.score.toFixed(3)}</td>
                        </tr>
                      ))}
                      {Object.entries(view.scored.unavailable || {}).map(([k, v]) => (
                        <tr key={k} className="na"><td>{k}</td><td colSpan={3}>unavailable — {String(v)}</td></tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {view.probes.map((p, i) => (
                  <div key={i} className="lv-probe">
                    <div className="lv-probeTop">
                      <b>{p.sel.probe_id}</b>
                      {p.res ? (p.res.blocked_by_policy ? <span className="pill pill-critical">blocked</span>
                        : <span className={`pill ${p.res.outcome ? "pill-info" : "pill-neutral"}`}>{p.res.outcome ?? "inconclusive"}</span>)
                        : <span className="pill pill-info">running…</span>}
                      {p.res && !p.res.blocked_by_policy && <span className="lv-muted">belief change {p.res.realized_gain_nats} nats (KL) · {Math.round(p.res.duration_ms)} ms</span>}
                      {p.res?.wasted && <span className="pill pill-major">wasted</span>}
                    </div>
                    <div className="lv-why">{p.sel.reason}</div>
                    {p.res?.stats?.cohorts && (
                      <div className="lv-stat">
                        {Object.entries(p.res.stats.cohorts).map(([v, c]: any) => (
                          <span key={v}>{v}: <b>{c.errors}/{c.requests}</b> errors ({pct(c.requests ? c.errors / c.requests : null)})</span>
                        ))}
                        <span>z = <b>{String(p.res.stats.z)}</b></span>
                      </div>
                    )}
                    {p.res?.stats?.current && (
                      <div className="lv-stat">
                        <span>now p95 <b>{ms(p.res.stats.current.p95_ms)}</b> vs baseline {ms(p.res.stats.baseline?.p95_ms)}</span>
                        <span>errors <b>{pct(p.res.stats.current.error_rate)}</b> of {p.res.stats.current.calls} calls</span>
                      </div>
                    )}
                  </div>
                ))}
                {view.diagnosis && (
                  <div className="lv-verdict ok">Diagnosis <b>{view.diagnosis.root_cause_id}</b> at {pct(view.diagnosis.posterior)} ·
                    confirmation <b>{view.diagnosis.confirmation}</b></div>
                )}
                {view.abstained && <div className="lv-verdict warn"><b>Abstained.</b> {view.abstained.reason}</div>}
              </div>
            </section>

            {/* ---- policy ---- */}
            <section className="card">
              <h2>Policy · OPA / Rego decisions (each sealed as evidence)</h2>
              <div className="body">
                {!view.policy.length && <div className="idle">no state-changing action has reached the gate</div>}
                {view.policy.map((p, i) => (
                  <div key={i} className={`lv-pol ${p.allow ? "allow" : "deny"}`}>
                    <span className={`pill ${p.allow ? "pill-healthy" : "pill-critical"}`}>{p.allow ? "ALLOW" : "DENY"}</span>
                    <b>{p.action}</b>
                    <span className="lv-muted">{p.stage}</span>
                    {!p.engine_available && <span className="pill pill-critical">engine down</span>}
                    {p.reasons?.length > 0 && <div className="lv-reasons">{p.reasons.join(" · ")}</div>}
                  </div>
                ))}
              </div>
            </section>

            {/* ---- remediation & verification ---- */}
            <section className="card">
              <h2>Remediation · executed against the running service</h2>
              <div className="body">
                {!view.proposed && <div className="idle">{view.abstained ? "no action — the system abstained" : "no remediation proposed yet"}</div>}
                {view.proposed && (
                  <div className="lv-rem">
                    <div><b>{view.proposed.action}</b> — {view.proposed.description}</div>
                    <div className="lv-muted">{view.proposed.reversible ? "reversible" : "irreversible"} · blast radius {view.proposed.blast_radius} · for {view.proposed.for_hypothesis}</div>
                  </div>
                )}
                {view.blocked && <div className="lv-verdict bad">Not executed — denied by policy.</div>}
                {view.executed && (
                  <div className="lv-change"><span>{view.executed.version_before}</span><span className="lv-arrow">→</span><span>{view.executed.version_after}</span>
                    <span className="lv-muted">service state changed</span></div>
                )}
                {view.verifying && !view.verification && <div className="idle">observing fresh telemetry…</div>}
                {view.verification && (
                  <div className={`lv-verify ${view.verification.recovered ? "ok" : "bad"}`}>
                    <div className="lv-verifyHead">{view.verification.recovered ? "RECOVERED" : "NOT RECOVERED"}</div>
                    <div className="lv-stat">
                      <span>before: <b>{pct(view.verification.before?.error_rate)}</b> errors</span>
                      <span>after: <b>{pct(view.verification.after?.error_rate)}</b> of {view.verification.after?.requests} requests</span>
                      <span>p95 <b>{ms(view.verification.after?.p95_ms)}</b></span>
                      <span className="lv-muted">criteria ≤ {pct(view.verification.criteria?.max_error_rate, 0)} errors, ≤ {view.verification.criteria?.max_p95_ms} ms</span>
                    </div>
                  </div>
                )}
                {view.revertTriggered && (
                  <div className="lv-revert">
                    <b>Auto-revert</b> — {view.revertTriggered.reason}
                    {view.revertExecuted && <div>reverted: service back on <b>{view.revertExecuted.version_after}</b> {view.revertExecuted.restored ? "(pre-action state restored)" : ""}</div>}
                  </div>
                )}
              </div>
            </section>
          </div>

          {/* ---- outcome ---- */}
          {view.closed && (
            <section className="card lv-outcome">
              <div className="body">
                <span className={`pill ${statusTone(view.closed.status)}`}>{statusLabel(view.closed.status)}</span>
                {view.escalated && <span className="lv-esc">{view.escalated.reason}</span>}
                <span className="lv-spacer" />
                {view.impact && <span className="lv-kv"><b>Customer impact</b> {view.impact.failed_requests} failed checkout requests</span>}
                {view.sealed && <span className="lv-kv"><b>Evidence chain</b> {view.sealed.leaves} leaves · {view.sealed.verified ? "signature verified ✓" : "NOT verified"} · {String(view.sealed.merkle_root).slice(0, 12)}…</span>}
              </div>
            </section>
          )}

          <section className="card">
            <h2>Timeline</h2>
            <div className="body lv-timeline">
              {ev.filter((e) => !QUIET.has(e.type)).map((e) => (
                <div key={e.seq} className={`lv-line t-${e.type}`}>
                  <span className="lv-t">+{view.detected ? (e.ts - view.detected.ts).toFixed(1) : "0.0"}s</span>
                  <span>{describe(e)}</span>
                </div>
              ))}
            </div>
          </section>
        </>
      )}

      {/* ---- history ---- */}
      <section className="card">
        <h2>Incident history</h2>
        <div className="body lv-scroll">
          {!state?.incidents?.length ? <div className="idle">none yet</div> : (
            <table className="lv-table">
              <thead><tr><th>incident</th><th>status</th><th>mode</th><th>diagnosis</th><th>probes (wasted)</th><th>remediation</th><th>recovered</th><th>reverted</th><th>closed in</th><th>chain</th></tr></thead>
              <tbody>
                {state.incidents.map((r: any) => (
                  <tr key={r.incident_id} className={r.incident_id === shownId ? "sel" : ""} onClick={() => setFocus(r.incident_id)}>
                    <td><button className="lv-link">{r.incident_id}</button></td>
                    <td><span className={`pill ${statusTone(r.status)}`}>{statusLabel(r.status)}</span></td>
                    <td>{r.mode}</td>
                    <td>{r.root_cause_id ? `${r.root_cause_id} (${r.confirmation})` : "—"}</td>
                    <td>{r.probes_run ?? "—"} ({r.probes_wasted ?? 0})</td>
                    <td>{r.remediation ? `${r.remediation}${r.remediation_executed ? "" : " · not executed"}` : "—"}</td>
                    <td>{r.recovered === null || r.recovered === undefined ? "—" : r.recovered ? "yes" : "no"}</td>
                    <td>{r.auto_reverted ? "yes" : "—"}</td>
                    <td>{r.time_to_close_s ?? "—"}s</td>
                    <td>{r.chain_verified ? "✓" : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {/* ---- environment ---- */}
      <section className="card lv-env">
        <h2>
          <button className="lv-link" onClick={() => setShowEnv(!showEnv)}>{showEnv ? "▾" : "▸"} Environment · fault injection</button>
        </h2>
        {showEnv && (
          <div className="body">
            <p className="lv-muted">Simulates the outside world breaking, through the target service's <code>/chaos</code> API.
              Incident Commander never calls that API and never sees this panel — it only sees the resulting telemetry.</p>
            <div className="lv-envRow">
              {SCENARIOS.map((s) => (
                <button key={s.id} className="btn" disabled={envBusy || !!active} onClick={() => inject(`scenario/${s.id}`)} title={s.expect}>{s.label}</button>
              ))}
              <button className="btn" disabled={envBusy} onClick={() => inject("clear")}>Clear faults</button>
              <button
                className={`btn ${loadgen?.running ? "btn-load-on" : "btn-load-off"}`}
                disabled={loadgenBusy}
                onClick={toggleLoadgen}
                title="In-process load generator against /shop/checkout at 60 rps. Without traffic, the SLO detector has nothing to observe."
              >{loadgenBusy ? "…" : (loadgen?.running ? `⏸ Stop traffic (${loadgen.rps.toFixed(0)} rps)` : "▶ Start traffic (60 rps)")}</button>
              <button
                className="btn btn-reset"
                disabled={resetBusy}
                onClick={resetToBaseline}
                title="Stop any active fault AND flip the target service back to healthy v6.09.0 baseline"
              >{resetBusy ? "Resetting…" : "⟲ Reset to baseline"}</button>
            </div>
            <div className="lv-muted">injected now: <b>{env?.scenario ?? "none"}</b> · traffic <b>{loadgen?.running ? `${loadgen.rps.toFixed(0)} rps` : "off"}</b>{active ? " · wait for the current incident to close before injecting another" : ""}</div>
          </div>
        )}
      </section>
    </div>
  );
}

function Stepper({ view, status }: { view: View; status?: string }) {
  const denied = !!view.blocked;
  const steps: { label: string; state: "done" | "active" | "skip" | "fail" | "todo" }[] = [
    { label: "Detected", state: view.detected ? "done" : "todo" },
    { label: "Investigate", state: view.diagnosis || view.abstained ? "done" : view.detected ? "active" : "todo" },
    { label: view.abstained ? "Abstained" : "Diagnosis", state: view.abstained ? "fail" : view.diagnosis ? "done" : "todo" },
    { label: "Policy", state: view.abstained ? "skip" : view.remPolicy ? (view.remPolicy.allow ? "done" : "fail") : view.diagnosis ? "active" : "todo" },
    { label: "Remediate", state: view.abstained || denied ? "skip" : view.executed ? "done" : view.remPolicy?.allow ? "active" : "todo" },
    { label: "Verify", state: view.abstained || denied ? "skip" : view.verification ? (view.verification.recovered ? "done" : "fail") : view.verifying ? "active" : "todo" },
    { label: "Auto-revert", state: view.revertExecuted ? "done" : view.revertTriggered ? "active" : view.verification || view.abstained || denied ? "skip" : "todo" },
    { label: "Closed", state: view.closed ? (status === "RESOLVED" ? "done" : "fail") : "todo" },
  ];
  return (
    <ol className="lv-steps">
      {steps.map((s, i) => <li key={i} className={s.state}><span className="lv-stepDot" />{s.label}</li>)}
    </ol>
  );
}

type View = ReturnType<typeof deriveView>;

function deriveView(ev: LiveEvent[]) {
  const last = (t: string) => { for (let i = ev.length - 1; i >= 0; i--) if (ev[i].type === t) return ev[i]; return undefined; };
  const all = (t: string) => ev.filter((e) => e.type === t);
  const hyps = all("hypothesis_generated");
  const beliefs = all("beliefs_updated");
  const latestBeliefs = beliefs[beliefs.length - 1];
  const posteriors: Record<string, number> = latestBeliefs?.posteriors || {};
  const leader = Object.keys(posteriors).sort((a, b) => posteriors[b] - posteriors[a])[0];
  const ruledOut: Record<string, boolean> = {};
  all("hypothesis_ruled_out").forEach((e) => { ruledOut[e.id] = true; });
  const selected = all("probe_selected");
  const executed = all("probe_executed");
  const policy = all("policy_decision");
  const meaningful = ev.filter((e) => !QUIET.has(e.type) && e.type !== "beliefs_updated" && e.type !== "agent_reasoned");
  return {
    detected: last("incident_detected"),
    hyps,
    posteriors,
    leader,
    confirmed: (latestBeliefs?.confirmed || []) as string[],
    ruledOut,
    entropy: beliefs.map((b) => b.entropy_nats as number),
    // the last scoring round that still had candidates — the final round after
    // the last probe is usually empty and would hide what the choice was between
    scored: all("probes_scored").filter((s) => s.candidates.length || Object.keys(s.unavailable || {}).length).pop(),
    lastSelected: selected[selected.length - 1],
    probes: selected.map((sel, i) => ({ sel, res: executed[i] })),
    diagnosis: last("diagnosis"),
    abstained: last("abstained"),
    proposed: last("remediation_proposed"),
    policy,
    remPolicy: policy.filter((p) => p.stage === "remediation").pop(),
    blocked: ev.find((e) => e.type === "action_blocked" && e.action !== "canary_probe"),
    executed: last("remediation_executed"),
    verifying: last("verification_started"),
    verification: last("verification_result"),
    revertTriggered: last("auto_revert_triggered"),
    revertExecuted: last("auto_revert_executed"),
    escalated: last("escalated"),
    impact: last("customer_impact_computed"),
    sealed: last("chain_sealed"),
    closed: last("incident_closed"),
    now: meaningful[meaningful.length - 1],
  };
}
