import type { AppConfig } from "./types";

// `window.CONFIG` is set by the classic <script src="/config.js"> in index.html,
// emitted same-origin by the FastAPI control server from environment variables.
declare global {
  interface Window {
    CONFIG?: AppConfig;
  }
}

const fallback: AppConfig = {
  backendUrl: "",
  tenantId: "",
  clientId: "",
  scope: "",
};

export const config: AppConfig = window.CONFIG ?? fallback;

// True when the control API advertised an Entra app registration. When false
// the runtime config failed to load (hard refresh needed) or auth is disabled.
export const authConfigured = Boolean(config.clientId && config.tenantId);
