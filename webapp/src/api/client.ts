import { config } from "../config";
import { getToken } from "../auth/msal";
import type { AccountInfo } from "@azure/msal-browser";
import type { FleetSnapshot, InjectKind } from "../types";

const TIMEOUT_MS = 5000;

/** Authenticated fetch against the control API for a given signed-in account. */
export class ApiClient {
  private readonly account: AccountInfo;

  constructor(account: AccountInfo) {
    this.account = account;
  }

  private async request(path: string, opts: RequestInit = {}): Promise<Response> {
    const token = await getToken(this.account);
    const headers = new Headers(opts.headers);
    headers.set("Authorization", `Bearer ${token}`);
    if (opts.body) headers.set("Content-Type", "application/json");

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      return await fetch(config.backendUrl + path, {
        ...opts,
        headers,
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  async getState(): Promise<Response> {
    return this.request("/api/state");
  }

  /** Rolling per-sensor history. `since` (epoch seconds) fetches only newer
   * samples; 0 backfills the whole retained window. */
  async getHistory(since = 0): Promise<Response> {
    return this.request(`/api/history?since=${encodeURIComponent(since)}`);
  }

  /** Injection windows + Fabric detections for the chart overlays. */
  async getEvents(): Promise<Response> {
    return this.request("/api/events");
  }

  async setRandom(machineId: string, enabled: boolean): Promise<Response> {
    return this.request(`/api/machines/${machineId}/random`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
  }

  /** Set the global anomaly-strength level (1..5) for the whole fleet. */
  async setLevel(level: number): Promise<Response> {
    return this.request("/api/level", {
      method: "POST",
      body: JSON.stringify({ level }),
    });
  }

  async inject(machineId: string, kind: InjectKind): Promise<Response> {
    return this.request(`/api/machines/${machineId}/inject`, {
      method: "POST",
      body: JSON.stringify({ kind }),
    });
  }

  async forceState(machineId: string, state: string | null): Promise<Response> {
    return this.request(`/api/machines/${machineId}/state`, {
      method: "POST",
      body: JSON.stringify({ state }),
    });
  }
}

export async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string };
    return body.detail || `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

export type { FleetSnapshot };
