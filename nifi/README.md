# NiFi flow — OTLP ingestion for Incident Commander

## What this proves

Real Apache NiFi ingesting **spec-compliant OpenTelemetry JSON** and routing
it to Incident Commander's ingestion endpoints. Same wire format an OTel
Collector produces in production. The only thing that changes in a real
deployment is the **source** the NiFi flow reads from.

## Flow topology

```
┌────────────────────┐
│ ListenHTTP :9091   │  ← corpus bridge / any OTel-compatible producer POSTs here
└──────────┬─────────┘
           │  OTLP JSON body
           ▼
┌────────────────────┐
│ RouteOnAttribute   │  → /v1/metrics  → InvokeHTTP → IC /ingest/otlp/v1/metrics
│  by path           │  → /v1/logs     → InvokeHTTP → IC /ingest/otlp/v1/logs
└──────────┬─────────┘  → default      → LogAttribute (drop)
           │
           ▼
┌────────────────────┐
│ InvokeHTTP         │  POST http://host.docker.internal:8000/ingest/otlp/...
└────────────────────┘
```

Every processor is stock NiFi — no custom NARs. The only thing our code
touches is the ingestion endpoint on the FastAPI side.

## Start the flow

**1. Bring NiFi up alongside the rest of the stack:**

```bash
docker compose up -d nifi
docker logs -f nifi 2>&1 | grep -i "single user credentials"
# Note the generated username + password from the log (NiFi >= 1.14
# writes them once to stdout, then they're persisted).
```

NiFi UI is at **https://localhost:8443/nifi/** — self-signed cert warning is
expected, click through.

**2. Import `otlp_ingestion.json`** via NiFi's UI:

* Upload button (top-right) → *Upload Process Group* → select `otlp_ingestion.json`
* Drop it onto the canvas → double-click to enter the group → *Start* (▶︎)

**3. Push events through it:**

```bash
# NiFi listens on :9091 (mapped in docker-compose). Push the same corpus payload
# that goes to the FastAPI endpoint directly:
python -m demo.otlp_bridge --endpoint http://localhost:9091

# Or a single spec-compliant curl:
curl -X POST http://localhost:9091/v1/metrics \
  -H 'Content-Type: application/json' \
  -d @demo/otlp_samples/checkout_5xx.json
```

**4. Verify NiFi forwarded to IC:**

```bash
curl -s http://localhost:8000/ingest/otlp/buffer | jq '.size'
```

Buffer count should equal what you pushed. NiFi's queue-count widgets on the
canvas will also visibly increment as the flow processes each POST.

## Why NiFi, not just direct HTTP

For the demo, the corpus bridge could POST directly to IC's `/ingest/otlp/*`
endpoints (and it does — see `demo/otlp_bridge.py`). NiFi is there for the
**production answer**:

* Multiple producers (real Collector, application-direct SDK, log shippers)
  fan into one durable intake point.
* Backpressure and retry queues survive an IC restart without dropping
  telemetry.
* The flow itself is versionable, auditable, and deployable independent of
  application code — the ops team owns it, not the app team.

Production doesn't POST from a script; it POSTs from a fleet of Collectors
into a NiFi cluster. This flow is that shape, scaled to one node.
