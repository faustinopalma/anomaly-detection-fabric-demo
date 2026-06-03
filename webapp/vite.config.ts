import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The control panel is served same-origin in production by the simulator's
// FastAPI control server (StaticFiles mount). In dev we run Vite on :5173 and
// proxy the API + runtime config to the local control server on :8080.
const BACKEND = process.env.SIM_BACKEND ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    // Relative asset URLs so the bundle works regardless of the mount path.
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/config.js": { target: BACKEND, changeOrigin: true },
      "/healthz": { target: BACKEND, changeOrigin: true },
    },
  },
});
