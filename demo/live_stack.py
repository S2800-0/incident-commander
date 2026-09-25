"""Start the whole LIVE stack with one command, stop it with Ctrl+C.

    python -m demo.live_stack            # OPA + Incident Commander + target + load + console
    python -m demo.live_stack --no-console

Processes (all bound to 127.0.0.1):
    OPA                 :8181   tools/opa(.exe) run --server policy/   (or IC_OPA_BIN)
    Incident Commander  :8000   uvicorn server.app:app   (live controller starts automatically)
    checkout-service    :9001   uvicorn mock.shop_svc:app
    load generator              python -m mock.loadgen --rps 60
    console             :5173   npm run dev   (in console/)

Nothing here injects a fault. Break the environment from the console's
"Environment · fault injection" panel, or:
    curl -X POST http://127.0.0.1:9001/chaos/scenario/bad_deploy
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _opa_bin() -> str:
    explicit = os.environ.get("IC_OPA_BIN")
    if explicit:
        return explicit
    local = ROOT / "tools" / ("opa.exe" if os.name == "nt" else "opa")
    if local.exists():
        return str(local)
    found = shutil.which("opa")
    if found:
        return found
    sys.exit("OPA binary not found. Put it at tools/opa(.exe), on PATH, or set IC_OPA_BIN "
             "(or run `docker compose up -d opa` and pass --external-opa).")


def _wait(url: str, name: str, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            print(f"  ✓ {name}  {url}", flush=True)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"{name} did not come up at {url}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-console", action="store_true")
    ap.add_argument("--external-opa", action="store_true", help="OPA already running on :8181 (e.g. docker compose)")
    ap.add_argument("--rps", type=float, default=60.0)
    args = ap.parse_args()

    py = sys.executable
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "IC_OPA_URL": "http://127.0.0.1:8181"}
    procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(name: str, cmd: list[str], cwd: Path = ROOT) -> None:
        print(f"starting {name}: {' '.join(cmd)}", flush=True)
        procs.append((name, subprocess.Popen(cmd, cwd=cwd, env=env, shell=(os.name == "nt" and cmd[0] == "npm"))))

    try:
        if not args.external_opa:
            spawn("opa", [_opa_bin(), "run", "--server", "--addr=127.0.0.1:8181", "--log-level=error", "policy/"])
        _wait("http://127.0.0.1:8181/health", "OPA")

        spawn("incident-commander", [py, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
                                     "--port", "8000", "--log-level", "warning"])
        _wait("http://127.0.0.1:8000/live/state", "Incident Commander")

        spawn("checkout-service", [py, "-m", "uvicorn", "mock.shop_svc:app", "--host", "127.0.0.1",
                                   "--port", "9001", "--log-level", "warning"])
        _wait("http://127.0.0.1:9001/ops/state", "checkout-service")

        spawn("loadgen", [py, "-m", "mock.loadgen", "--rps", str(args.rps)])

        if not args.no_console:
            spawn("console", ["npm", "run", "dev", "--", "--host", "127.0.0.1"], cwd=ROOT / "console")
            _wait("http://127.0.0.1:5173", "console", timeout_s=60)

        print("\nLive stack is up. Console: http://127.0.0.1:5173 (Live tab)")
        print("Wait ~45s for a clean telemetry baseline before injecting a fault. Ctrl+C to stop.\n", flush=True)
        while all(p.poll() is None for _, p in procs):
            time.sleep(1)
        dead = [n for n, p in procs if p.poll() is not None]
        print(f"process exited: {dead} — stopping the stack", flush=True)
    except KeyboardInterrupt:
        print("\nstopping…", flush=True)
    finally:
        for name, p in reversed(procs):
            if p.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
                else:
                    p.send_signal(signal.SIGTERM)
        for _, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
