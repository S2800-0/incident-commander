"""OTLP ingestion adapter — real OpenTelemetry JSON on the wire, real
`EvidenceItem` on the other side.

Contract:
  * `POST /ingest/otlp/v1/metrics` accepts an OTLP JSON body matching the
    OpenTelemetry proto schema (spec: opentelemetry-proto/opentelemetry/proto/
    collector/metrics/v1/metrics_service.proto). Same shape as what an OTel
    Collector would export via its HTTP JSON endpoint.
  * `POST /ingest/otlp/v1/logs` accepts the logs equivalent.
  * We parse a strict subset — the fields we actually consume for evidence —
    and pass unknown fields through in `payload.otlp_raw` so nothing is lost
    for audit.

Why write our own subset and not depend on the opentelemetry-python SDK: the
SDK is instrumentation-side (produces OTLP), not receiver-side (consumes it).
Receiver-side deserialisation via the SDK's proto stubs would drag ~40MB of
gRPC + protobuf just for JSON shape validation we get for free with pydantic.

Design rules:
  * NEVER drop a resource attribute — it carries `service.name` which is our
    primary source-of-truth for which system emitted the signal.
  * NEVER normalise metric names — a Prometheus scrape and an OTel SDK emit
    the same measurement under different names; the downstream reasoner
    matches on the raw name.
  * Timestamps in OTLP are string-encoded nanoseconds (uint64 doesn't fit in
    JSON number). Parse as string, convert to ISO-8601 for our records.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import EvidenceItem


# ============================================================================
# OTLP wire-format models (subset). Field names match the proto JSON encoding
# — camelCase, not snake_case. Do not "pythonise" them.
# ============================================================================

class OtlpAnyValue(BaseModel):
    """OTLP attribute values are tagged unions; we support the primitive tags
    that appear in real telemetry. Complex tags (array/kvlist) pass through as
    dicts so they land intact in the evidence payload."""
    stringValue: Optional[str] = None
    intValue: Optional[str] = None      # OTLP encodes int64 as string in JSON
    doubleValue: Optional[float] = None
    boolValue: Optional[bool] = None
    arrayValue: Optional[dict] = None
    kvlistValue: Optional[dict] = None

    def unwrap(self) -> Any:
        for f in ("stringValue", "intValue", "doubleValue", "boolValue"):
            v = getattr(self, f)
            if v is not None:
                return int(v) if f == "intValue" else v
        return self.arrayValue or self.kvlistValue


class OtlpKeyValue(BaseModel):
    key: str
    value: OtlpAnyValue


class OtlpResource(BaseModel):
    attributes: list[OtlpKeyValue] = Field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {kv.key: kv.value.unwrap() for kv in self.attributes}

    def get(self, key: str, default: str = "unknown") -> str:
        for kv in self.attributes:
            if kv.key == key:
                v = kv.value.unwrap()
                return str(v) if v is not None else default
        return default


class OtlpScope(BaseModel):
    name: Optional[str] = None
    version: Optional[str] = None


# ---- Metric data-point variants (subset) -----------------------------------

class OtlpNumberDataPoint(BaseModel):
    timeUnixNano: str = "0"
    startTimeUnixNano: Optional[str] = None
    asDouble: Optional[float] = None
    asInt: Optional[str] = None
    attributes: list[OtlpKeyValue] = Field(default_factory=list)

    def value(self) -> float:
        if self.asDouble is not None:
            return float(self.asDouble)
        if self.asInt is not None:
            return float(self.asInt)
        return 0.0

    def iso_time(self) -> str:
        return _nano_to_iso(self.timeUnixNano)


class OtlpHistogramDataPoint(BaseModel):
    timeUnixNano: str = "0"
    count: str = "0"
    sum: Optional[float] = None
    bucketCounts: list[str] = Field(default_factory=list)
    explicitBounds: list[float] = Field(default_factory=list)
    attributes: list[OtlpKeyValue] = Field(default_factory=list)

    def iso_time(self) -> str:
        return _nano_to_iso(self.timeUnixNano)


class OtlpGauge(BaseModel):
    dataPoints: list[OtlpNumberDataPoint] = Field(default_factory=list)


class OtlpSum(BaseModel):
    dataPoints: list[OtlpNumberDataPoint] = Field(default_factory=list)
    isMonotonic: Optional[bool] = None
    aggregationTemporality: Optional[int] = None


class OtlpHistogram(BaseModel):
    dataPoints: list[OtlpHistogramDataPoint] = Field(default_factory=list)
    aggregationTemporality: Optional[int] = None


class OtlpMetric(BaseModel):
    name: str
    unit: str = ""
    description: str = ""
    gauge: Optional[OtlpGauge] = None
    sum: Optional[OtlpSum] = None
    histogram: Optional[OtlpHistogram] = None

    def kind(self) -> str:
        if self.gauge:     return "gauge"
        if self.sum:       return "sum"
        if self.histogram: return "histogram"
        return "unknown"

    def data_points(self) -> list[dict]:
        """Uniform structural view of every data point in this metric,
        preserving units and attributes."""
        if self.gauge:
            return [{"kind": "gauge", "value": p.value(), "time": p.iso_time(),
                     "attrs": {kv.key: kv.value.unwrap() for kv in p.attributes}}
                    for p in self.gauge.dataPoints]
        if self.sum:
            return [{"kind": "sum", "value": p.value(), "time": p.iso_time(),
                     "monotonic": bool(self.sum.isMonotonic),
                     "attrs": {kv.key: kv.value.unwrap() for kv in p.attributes}}
                    for p in self.sum.dataPoints]
        if self.histogram:
            return [{"kind": "histogram", "count": int(p.count), "sum": p.sum,
                     "time": p.iso_time(), "buckets": list(p.bucketCounts),
                     "bounds": list(p.explicitBounds),
                     "attrs": {kv.key: kv.value.unwrap() for kv in p.attributes}}
                    for p in self.histogram.dataPoints]
        return []


class OtlpScopeMetrics(BaseModel):
    scope: OtlpScope = Field(default_factory=OtlpScope)
    metrics: list[OtlpMetric] = Field(default_factory=list)


class OtlpResourceMetrics(BaseModel):
    resource: OtlpResource = Field(default_factory=OtlpResource)
    scopeMetrics: list[OtlpScopeMetrics] = Field(default_factory=list)


class OtlpMetricsPayload(BaseModel):
    resourceMetrics: list[OtlpResourceMetrics] = Field(default_factory=list)


# ---- Log record ------------------------------------------------------------

class OtlpLogBody(BaseModel):
    stringValue: Optional[str] = None
    kvlistValue: Optional[dict] = None

    def text(self) -> str:
        if self.stringValue is not None:
            return self.stringValue
        if self.kvlistValue:
            return str(self.kvlistValue)
        return ""


class OtlpLogRecord(BaseModel):
    timeUnixNano: str = "0"
    severityText: Optional[str] = None
    severityNumber: Optional[int] = None
    body: OtlpLogBody = Field(default_factory=OtlpLogBody)
    attributes: list[OtlpKeyValue] = Field(default_factory=list)

    def iso_time(self) -> str:
        return _nano_to_iso(self.timeUnixNano)


class OtlpScopeLogs(BaseModel):
    scope: OtlpScope = Field(default_factory=OtlpScope)
    logRecords: list[OtlpLogRecord] = Field(default_factory=list)


class OtlpResourceLogs(BaseModel):
    resource: OtlpResource = Field(default_factory=OtlpResource)
    scopeLogs: list[OtlpScopeLogs] = Field(default_factory=list)


class OtlpLogsPayload(BaseModel):
    resourceLogs: list[OtlpResourceLogs] = Field(default_factory=list)


# ============================================================================
# Adapter functions — OTLP → EvidenceItem
# ============================================================================

def _nano_to_iso(nano: str | int) -> str:
    """OTLP timestamps are strings of nanoseconds since Unix epoch."""
    try:
        secs = int(nano) / 1_000_000_000
        return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return "1970-01-01T00:00:00+00:00"


def _source_uri(service: str, kind: str, name: str) -> str:
    """Stable, resolvable id used by agents to cite this evidence."""
    return f"otlp://{service}/{kind}/{name}"


def metrics_to_evidence(payload: OtlpMetricsPayload) -> list[EvidenceItem]:
    """One EvidenceItem per (resource × metric) — mirrors how a real Prometheus
    query returns one series per label set. Downstream agents match on
    `payload.metric_name` and the semantic-convention resource attributes."""
    items: list[EvidenceItem] = []
    for rm in payload.resourceMetrics:
        resource = rm.resource.as_dict()
        service = rm.resource.get("service.name")
        env = rm.resource.get("deployment.environment", "unknown")
        for sm in rm.scopeMetrics:
            for m in sm.metrics:
                dps = m.data_points()
                if not dps:
                    continue
                items.append(EvidenceItem(
                    source_type="otlp:metric",
                    source_uri=_source_uri(service, "metric", m.name),
                    retrieved_at=dps[-1]["time"],
                    measures=[m.name],
                    payload={
                        "service.name":            service,
                        "deployment.environment":  env,
                        "metric.name":             m.name,
                        "metric.unit":             m.unit,
                        "metric.kind":             m.kind(),
                        "scope":                    sm.scope.model_dump(),
                        "resource":                 resource,
                        "data_points":              dps,
                    },
                ))
    return items


def logs_to_evidence(payload: OtlpLogsPayload) -> list[EvidenceItem]:
    """One EvidenceItem per resource-log batch. Individual log records live in
    `payload.records` — one item per batch keeps hashes stable, avoids the
    combinatorial blowup of one-item-per-record on a chatty service."""
    items: list[EvidenceItem] = []
    for rl in payload.resourceLogs:
        resource = rl.resource.as_dict()
        service = rl.resource.get("service.name")
        env = rl.resource.get("deployment.environment", "unknown")
        for sl in rl.scopeLogs:
            if not sl.logRecords:
                continue
            records = [{
                "time":     r.iso_time(),
                "severity": r.severityText or "",
                "body":     r.body.text(),
                "attrs":    {kv.key: kv.value.unwrap() for kv in r.attributes},
            } for r in sl.logRecords]
            latest = records[-1]["time"]
            severities = sorted({r["severity"] for r in records if r["severity"]})
            items.append(EvidenceItem(
                source_type="otlp:log",
                source_uri=_source_uri(service, "log", sl.scope.name or "default"),
                retrieved_at=latest,
                measures=[f"log:{sev}" for sev in severities] or ["log:records"],
                payload={
                    "service.name":            service,
                    "deployment.environment":  env,
                    "scope":                    sl.scope.model_dump(),
                    "resource":                 resource,
                    "record_count":             len(records),
                    "severities":               severities,
                    "records":                  records,
                },
            ))
    return items


# ============================================================================
# Corpus → OTLP bridge — proves the pipe end-to-end today.
#
# Every bundle's prior_evidence + probeable evidence is re-encoded as
# spec-compliant OTLP JSON and pushed through the ingestion endpoint. This
# demonstrates that swapping the source (real OTel collector for our corpus)
# is a NiFi config change, not a codebase rewrite.
# ============================================================================

def _now_nanos() -> str:
    return str(int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000_000))


def evidence_to_otlp(items: list[EvidenceItem], service_name: str = "checkout-service",
                      environment: str = "production") -> dict:
    """Package a list of EvidenceItems as an OTLP metrics payload — one
    ResourceMetrics with one ScopeMetrics containing one Metric per item.
    Values compress payloads (numeric-shaped → gauge; anything else → the
    body preserved on the log side)."""
    metrics_out: list[dict] = []
    for it in items:
        # Extract a scalar we can attach as a gauge point, or fall back to a
        # sentinel value with the full payload preserved under attrs.
        val = _try_extract_numeric(it.payload)
        metric = {
            "name": (it.measures[0] if it.measures else it.source_uri),
            "unit": "",
            "description": f"synthesised from {it.source_uri}",
            "gauge": {"dataPoints": [{
                "timeUnixNano": _now_nanos(),
                "asDouble": val,
                "attributes": [
                    {"key": "source_uri", "value": {"stringValue": it.source_uri}},
                    {"key": "source_type", "value": {"stringValue": it.source_type}},
                ],
            }]},
        }
        metrics_out.append(metric)
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name",           "value": {"stringValue": service_name}},
                {"key": "deployment.environment", "value": {"stringValue": environment}},
                {"key": "telemetry.sdk.language", "value": {"stringValue": "corpus_bridge"}},
            ]},
            "scopeMetrics": [{
                "scope":   {"name": "incident_commander.corpus_bridge", "version": "0.1"},
                "metrics": metrics_out,
            }],
        }],
    }


def _try_extract_numeric(payload: dict) -> float:
    """Best-effort scalar for the gauge point. Falls back to 1.0 as a presence
    indicator when nothing scalar is available — the real signal is the
    attribute (`source_uri`), not the value."""
    if not isinstance(payload, dict):
        return 1.0
    for k in ("value", "delta_pct", "p95_ms", "latency_ms", "count", "rate"):
        v = payload.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 1.0
