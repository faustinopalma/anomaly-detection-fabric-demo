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
  server_time?: number;
  machines: Machine[];
}

// Columnar per-sensor history from GET /api/history. `t` holds epoch-second
// timestamps; each `series[sensor]` array is parallel to `t`.
export interface MachineHistory {
  machine_id: string;
  t: number[];
  series: Record<string, (number | null)[]>;
}

export interface HistoryResponse {
  server_time: number;
  window_s: number;
  machines: MachineHistory[];
}

export type InjectKind = "spike" | "drift" | "stuck";

export type ConnStatus = "unknown" | "connecting" | "online" | "offline" | "standby";
