"""Unit tests for the live-mode engine's pure parts. Run: python -m pytest tests -q"""
import math
import time

from ic.live import beliefs
from ic.live.catalog import OBSERVABLES, generate_hypotheses
from ic.live.probes import two_proportion_z
from ic.live.telemetry import TelemetryStore, quantile_from_buckets
from ic.otlp import OtlpMetricsPayload

BIN = ("a", "b")


# --- beliefs ---------------------------------------------------------------------

def test_eig_is_zero_when_hypotheses_agree():
    p = {"H1": 0.5, "H2": 0.5}
    assert beliefs.expected_information_gain(p, {"H1": "a", "H2": "a"}, BIN) == 0.0


def test_eig_of_perfect_binary_split_is_ln2():
    p = {"H1": 0.5, "H2": 0.5}
    eig = beliefs.expected_information_gain(p, {"H1": "a", "H2": "b"}, BIN, eps=0.0)
    assert math.isclose(eig, math.log(2), rel_tol=1e-9)


def test_eig_positive_when_only_one_hypothesis_commits():
    # The replay divergence count scores this 0; formal EIG does not.
    p = {"H1": 0.5, "H2": 0.5}
    assert beliefs.expected_information_gain(p, {"H1": "a", "H2": None}, BIN) > 0.05


def test_update_moves_mass_toward_matching_prediction():
    p = beliefs.update({"H1": 0.5, "H2": 0.5}, {"H1": "a", "H2": "b"}, "a", BIN)
    assert p["H1"] > 0.9 and math.isclose(sum(p.values()), 1.0)


def test_update_leaves_agnostic_pair_undecided():
    # Correlated-degradation shape: each hypothesis's signature is confirmed in turn.
    p = {"H_db": 0.5, "H_inv": 0.5}
    p = beliefs.update(p, {"H_db": "degraded", "H_inv": None}, "degraded", ("degraded", "healthy"))
    p = beliefs.update(p, {"H_db": None, "H_inv": "degraded"}, "degraded", ("degraded", "healthy"))
    assert abs(p["H_db"] - p["H_inv"]) < 1e-9


def test_probe_that_reveals_ambiguity_counts_as_information():
    # Entropy rises (0.64 → 0.69 nats) but beliefs moved materially: not wasted.
    before = {"H_db": 0.655, "H_inv": 0.345}
    after = {"H_db": 0.5, "H_inv": 0.5}
    assert beliefs.entropy(after) > beliefs.entropy(before)
    assert beliefs.kl_divergence(after, before) > 0.01


def test_probe_that_changes_nothing_has_zero_kl():
    p = {"H_db": 0.95, "H_inv": 0.05}
    assert beliefs.kl_divergence(p, dict(p)) == 0.0


# --- telemetry ----------------------------------------------------------------------

def test_quantile_interpolates_inside_bucket():
    # 100 samples all in (10, 25] → p50 halfway through that bucket
    q = quantile_from_buckets(0.5, [0, 100, 0], [10, 25])
    assert math.isclose(q, 17.5)


def _payload(t: float, version: str, ok: int, err: int) -> OtlpMetricsPayload:
    ns = str(int(t * 1e9))
    kv = lambda k, v: {"key": k, "value": {"stringValue": v}}
    return OtlpMetricsPayload.model_validate({"resourceMetrics": [{
        "resource": {"attributes": [kv("service.name", "checkout-service")]},
        "scopeMetrics": [{"metrics": [{"name": "http.server.request.count", "sum": {"dataPoints": [
            {"timeUnixNano": ns, "asInt": str(ok), "attributes": [kv("service.version", version), kv("http.response.status_class", "2xx")]},
            {"timeUnixNano": ns, "asInt": str(err), "attributes": [kv("service.version", version), kv("http.response.status_class", "5xx")]},
        ]}}]}]}]})


def test_store_error_rate_and_cohorts():
    s = TelemetryStore()
    now = time.time()
    s.ingest(_payload(now - 2, "v2", 70, 30))
    s.ingest(_payload(now - 1, "v1", 50, 0))
    stats = s.request_stats(now - 5, now)
    assert stats["requests"] == 150 and stats["errors"] == 30
    rows = s.errors_by_version(now - 5, now)
    assert rows["v2"] == {"requests": 100, "errors": 30} and rows["v1"]["errors"] == 0


def test_two_proportion_z_detects_real_gap():
    assert two_proportion_z(30, 100, 0, 50) > 3
    assert abs(two_proportion_z(5, 100, 3, 60)) < 3


def _dep_payload(t: float, dep: str, n: int, bucket: int) -> OtlpMetricsPayload:
    """n calls to `dep`, all in histogram bucket index `bucket` of the target's bounds."""
    bounds = [5, 10, 25, 50, 75, 100, 150, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000]
    counts = ["0"] * (len(bounds) + 1)
    counts[bucket] = str(n)
    ns = str(int(t * 1e9))
    kv = lambda k, v: {"key": k, "value": {"stringValue": v}}
    return OtlpMetricsPayload.model_validate({"resourceMetrics": [{
        "resource": {"attributes": [kv("service.name", "checkout-service")]},
        "scopeMetrics": [{"metrics": [
            {"name": "dependency.client.request.count", "sum": {"dataPoints": [
                {"timeUnixNano": ns, "asInt": str(n), "attributes": [kv("peer.service", dep), kv("outcome", "ok")]}]}},
            {"name": "dependency.client.duration", "histogram": {"dataPoints": [
                {"timeUnixNano": ns, "count": str(n), "sum": 1.0, "bucketCounts": counts, "explicitBounds": bounds,
                 "attributes": [kv("peer.service", dep)]}]}},
        ]}]}]})


def _dependency_outcome(blip_seconds_ago: set[int]) -> str:
    from ic.live.catalog import PROBES
    from ic.live.probes import dependency_probe
    store = TelemetryStore()
    now = time.time()
    for k in range(60, -1, -1):            # one export per second, 60s of history
        bucket = 7 if k in blip_seconds_ago else 2   # (150,200] ms vs (10,25] ms
        store.ingest(_dep_payload(now - k, "inventory-service", 60, bucket))
    spec = next(p for p in PROBES if p.dependency == "inventory-service")
    return dependency_probe(spec, store, "LIVE-TEST", fired_at=now - 2).outcome


def test_transient_blip_that_already_ended_reads_healthy():
    # blip only in the older half of the 4s window (the LIVE-0012 / LIVE-0052 shape)
    assert _dependency_outcome({3, 2}) == "dependency_healthy"


def test_sustained_degradation_reads_degraded():
    assert _dependency_outcome({3, 2, 1, 0}) == "dependency_degraded"


# --- hypothesis generation ------------------------------------------------------------

DEPS = ["orders-db", "inventory-service"]


def test_recent_release_generates_deploy_hypothesis_with_signature():
    fired = time.time()
    hyps = generate_hypotheses("checkout-service", fired, {"version": "v6.10.0", "kind": "code"}, fired - 30, DEPS)
    ids = {h.id for h in hyps}
    assert ids == {"H_deploy", "H_orders_db", "H_inventory"}
    deploy = next(h for h in hyps if h.id == "H_deploy")
    assert deploy.signature() == {"version_cohort_errors"}
    assert all(po.observable in OBSERVABLES for h in hyps for po in h.hyp.predicted_observations)


def test_old_release_generates_no_deploy_hypothesis():
    fired = time.time()
    hyps = generate_hypotheses("checkout-service", fired, {"version": "v6.09.0", "kind": "code"}, fired - 3 * 3600, DEPS)
    assert "H_deploy" not in {h.id for h in hyps}


def test_deploy_hypothesis_is_classified_as_rollback_by_replay_reasoner():
    from ic.reasoner import classify, is_rollback_hypothesis
    fired = time.time()
    hyps = {h.id: h for h in generate_hypotheses("checkout-service", fired, {"version": "v6.10.0", "kind": "code"}, fired - 30, DEPS)}
    assert is_rollback_hypothesis(hyps["H_deploy"].hyp.claim)
    assert not is_rollback_hypothesis(hyps["H_orders_db"].hyp.claim)
    assert classify(hyps["H_orders_db"].hyp.claim).upstream
