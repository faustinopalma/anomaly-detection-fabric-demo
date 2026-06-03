// Shape of the runtime config injected by the control API as `window.CONFIG`
// (served from /config.js, same origin). Values are public (not secrets).
export interface AppConfig {
  backendUrl: string;
  tenantId: string;
  clientId: string;
  scope: string;
}

// A single machine's snapshot as returned by GET /api/state.
export interface Machine {
  machine_id: string;
  state: string;
  active: boolean;
  active_anomaly?: string | null;
  random_enabled: boolean;
  forced_state?: string | null;
  valid_states?: string[];
  sensors?: string[];
  last_sample?: Record<string, number | null>;
}

export interface FleetSnapshot {
  machine_count: number;
  uptime_s: number;
  machines: Machine[];
}

export type InjectKind = "spike" | "drift" | "stuck";

export type ConnStatus = "unknown" | "connecting" | "online" | "offline" | "standby";
