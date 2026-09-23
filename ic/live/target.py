"""Client for the target service's deploy/actuator API (`/ops/*`).

Incident Commander reaches the running service ONLY through this module and
through telemetry. It has no method for the `/chaos` API — the agent cannot
see or undo the environment's faults, only react to their symptoms.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

TARGET_URL = os.environ.get("IC_TARGET_URL", "http://127.0.0.1:9001")


class TargetError(RuntimeError):
    pass


class TargetClient:
    def __init__(self, base_url: str = TARGET_URL, timeout_s: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_s)

    def _get(self, path: str):
        try:
            r = self._client.get(f"{self.base_url}{path}")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise TargetError(f"GET {path} failed: {e}") from e

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self._client.post(f"{self.base_url}{path}", json=body)
        except httpx.HTTPError as e:
            raise TargetError(f"POST {path} failed: {e}") from e
        out = {"http_status": r.status_code}
        try:
            out["body"] = r.json()
        except ValueError:
            out["body"] = {"raw": r.text[:500]}
        if r.status_code >= 400:
            raise TargetError(f"POST {path} returned {r.status_code}: {out['body']}")
        return out

    # --- reads -------------------------------------------------------------------

    def state(self) -> dict:
        return self._get("/ops/state")

    def deployments(self, limit: int = 10) -> list[dict]:
        return self._get(f"/ops/deployments?limit={limit}")

    # --- actions -----------------------------------------------------------------

    def canary(self, incident_id: str, version: str, pct: float, duration_s: float) -> dict:
        return self._post("/ops/canary", {"incident_id": incident_id, "version": version,
                                          "pct": pct, "duration_s": duration_s})

    def execute(self, action: str, incident_id: str, reason: str) -> dict:
        ctx = {"incident_id": incident_id, "reason": reason}
        if action == "rollback_deploy":
            return self._post("/ops/rollback", ctx)
        if action == "enable_inventory_fallback":
            return self._post("/ops/inventory-fallback", {**ctx, "enabled": True})
        if action == "failover_database":
            return self._post("/ops/failover-database", ctx)
        raise TargetError(f"no executor for action {action!r}")

    def revert(self, action: str, incident_id: str, before_state: dict, reason: str) -> dict:
        """Restore the state that existed before `action` ran."""
        ctx = {"incident_id": incident_id, "reason": reason}
        if action == "rollback_deploy":
            return self._post("/ops/set-version", {**ctx, "version": before_state["active_version"]})
        if action == "enable_inventory_fallback":
            return self._post("/ops/inventory-fallback", {**ctx, "enabled": bool(before_state["inventory_fallback"])})
        raise TargetError(f"action {action!r} has no revert — it should never have been allowed")

    def reachable(self) -> Optional[dict]:
        try:
            return self.state()
        except TargetError:
            return None
