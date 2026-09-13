import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy API + WebSocket to the FastAPI server on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/incidents": "http://localhost:8000",
      "/harness": "http://localhost:8000",
      "/investigate": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
      // Target service mock — see mock/shop_svc.py. Rewrites /api/shop/*
      // → :9000/shop/* so the console can fetch state without CORS.
      "/api/shop": { target: "http://localhost:9001", rewrite: (p) => p.replace(/^\/api/, "") },
    },
  },
});
