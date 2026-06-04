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
  level?: number;
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

// Anomaly strength level (1 = mild, 5 = severe). A single global level applies
// to every machine.
export type AnomalyLevel = 1 | 2 | 3 | 4 | 5;

// An injected anomaly interval, surfaced so the charts can shade the affected
// period. `start`/`end` are epoch seconds on the server clock.
export interface InjectionWindow {
  id: number;
  kind: InjectKind;
  sensor: string;
  start: number;
  end: number;
  level: number;
  source: "manual" | "random";
}

// A detection the Fabric model wrote to the `anomalies` table. `sensor_id` is
// null for multivariate models (applies to the whole machine). `t` is epoch
// seconds (UTC).
export interface Detection {
  machine_id: string;
  t: number;
  score: number;
  model_name: string;
  sensor_id: string | null;
}

export interface MachineEvents {
  machine_id: string;
  injections: InjectionWindow[];
}

export interface EventsResponse {
  server_time: number;
  window_s: number;
  level: number;
  machines: MachineEvents[];
  detections: Detection[];
}

export type InjectKind = "spike" | "drift" | "stuck";

export type ConnStatus = "unknown" | "connecting" | "online" | "offline" | "standby";
