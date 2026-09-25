import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy API + WebSocket to the FastAPI server on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/incidents": "http://127.0.0.1:8000",
      "/harness": "http://127.0.0.1:8000",
      "/investigate": "http://127.0.0.1:8000",
      "/policy": "http://127.0.0.1:8000",
      "/live": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      // Target service mock — see mock/shop_svc.py. Rewrites /api/shop/*
      // → :9000/shop/* so the console can fetch state without CORS.
      "/api/shop": { target: "http://127.0.0.1:9001", rewrite: (p) => p.replace(/^\/api/, "") },
      // Environment fault injection (live mode) — the target's chaos API, never IC.
      "/api/chaos": { target: "http://127.0.0.1:9001", rewrite: (p) => p.replace(/^\/api/, "") },
      "/api/ops": { target: "http://127.0.0.1:9001", rewrite: (p) => p.replace(/^\/api/, "") },
      "/api/loadgen": { target: "http://127.0.0.1:9001", rewrite: (p) => p.replace(/^\/api/, "") },
    },
  },
});
