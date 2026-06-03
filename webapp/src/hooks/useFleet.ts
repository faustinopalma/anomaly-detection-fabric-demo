import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api/client";
import type { ConnStatus, FleetSnapshot, HistoryResponse } from "../types";

const POLL_MS = 2000;
// Chart window. Must match the server-side retention (history_window_s) so a
// reconnect can backfill the full visible range.
const CHART_WINDOW_MS = 5 * 60 * 1000;

export interface SeriesPoint {
  t: number;
  v: number | null;
}

interface Ring {
  t: number[];
  s: Record<string, (number | null)[]>;
}

interface FleetState {
  snapshot: FleetSnapshot | null;
  status: ConnStatus;
  lastUpdated: number | null;
  offlineDetail: string;
  getSeries: (machineId: string, sensor: string) => SeriesPoint[];
}

/**
 * Poll the control API while `active`. The fleet snapshot drives the controls;
 * the charts are fed from the server-side history endpoint so they stay
 * continuous and capture every sample at the simulator's tick rate (not just
 * the 2 s poll). Timestamps come from the server, so backgrounding the tab and
 * returning simply backfills the whole window — no gaps or jumps.
 */
export function useFleet(
  client: ApiClient | null,
  active: boolean,
  chartsOn: boolean,
): FleetState {
  const [snapshot, setSnapshot] = useState<FleetSnapshot | null>(null);
  const [status, setStatus] = useState<ConnStatus>("unknown");
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [offlineDetail, setOfflineDetail] = useState("");
  // Per-machine rolling history in client-adjusted ms. Mutated in place; a
  // counter bump triggers re-render so the charts pick up new points.
  const ringRef = useRef<Map<string, Ring>>(new Map());
  // High-water mark (server epoch seconds) of the newest sample we hold, used
  // as `since` for incremental fetches. 0 forces a full-window backfill.
  const sinceRef = useRef(0);
  const [, setTick] = useState(0);

  const mergeHistory = useCallback((h: HistoryResponse, full: boolean) => {
    // Align server timestamps to the local clock so the chart's "now" window
    // (Date.now based) lines up regardless of clock skew.
    const offset = Date.now() - h.server_time * 1000;
    const cutoff = Date.now() - CHART_WINDOW_MS;
    let maxT = full ? 0 : sinceRef.current;

    for (const m of h.machines) {
      let ring = ringRef.current.get(m.machine_id);
      if (!ring || full) {
        ring = { t: [], s: {} };
        ringRef.current.set(m.machine_id, ring);
      }
      const n = m.t.length;
      for (let i = 0; i < n; i++) {
        const ts = m.t[i];
        if (ts > maxT) maxT = ts;
        ring.t.push(ts * 1000 + offset);
        for (const sensor of Object.keys(m.series)) {
          if (!ring.s[sensor]) ring.s[sensor] = [];
          const v = m.series[sensor][i];
          ring.s[sensor].push(v == null ? null : Number(v));
        }
      }
      // Drop points older than the window.
      let drop = 0;
      while (drop < ring.t.length && ring.t[drop] < cutoff) drop++;
      if (drop > 0) {
        ring.t.splice(0, drop);
        for (const key of Object.keys(ring.s)) ring.s[key].splice(0, drop);
      }
    }
    sinceRef.current = maxT;
    setTick((x) => x + 1);
  }, []);

  useEffect(() => {
    if (!active || !client) {
      setStatus(active ? "unknown" : "standby");
      return;
    }

    let cancelled = false;
    setStatus("connecting");
    // Fresh activation (incl. returning from a hidden tab): backfill the full
    // window on the next history fetch.
    sinceRef.current = 0;
    ringRef.current.clear();

    const poll = async () => {
      try {
        const res = await client.getState();
        if (cancelled) return;
        if (res.status === 401 || res.status === 403) {
          setStatus("offline");
          setOfflineDetail("Access denied — your account is not authorized for this app.");
          return;
        }
        if (!res.ok) {
          setStatus("offline");
          setOfflineDetail(`Control API returned HTTP ${res.status}.`);
          return;
        }
        const snap = (await res.json()) as FleetSnapshot;
        if (cancelled) return;
        setSnapshot(snap);
        setStatus("online");
        setLastUpdated(Date.now());
        setOfflineDetail("");

        if (chartsOn) {
          const full = sinceRef.current === 0;
          const hres = await client.getHistory(sinceRef.current);
          if (cancelled || !hres.ok) return;
          const hist = (await hres.json()) as HistoryResponse;
          if (cancelled) return;
          mergeHistory(hist, full);
        }
      } catch {
        if (cancelled) return;
        setStatus("offline");
        setOfflineDetail(
          "The simulator may be stopped to save cost. The panel is inactive until it responds.",
        );
      }
    };

    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [client, active, chartsOn, mergeHistory]);

  // Clear accumulated history when charts are switched off.
  useEffect(() => {
    if (!chartsOn) {
      ringRef.current.clear();
      sinceRef.current = 0;
    }
  }, [chartsOn]);

  const getSeries = useCallback((machineId: string, sensor: string): SeriesPoint[] => {
    const ring = ringRef.current.get(machineId);
    if (!ring) return [];
    const arr = ring.s[sensor];
    if (!arr) return [];
    return ring.t.map((t, i) => ({ t, v: arr[i] ?? null }));
  }, []);

  return { snapshot, status, lastUpdated, offlineDetail, getSeries };
}
