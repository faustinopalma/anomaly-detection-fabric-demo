import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api/client";
import type { ConnStatus, FleetSnapshot } from "../types";

const POLL_MS = 2000;
// Rolling client-side chart window, built entirely from the /api/state poll
// (no extra server stream), so it adds zero backend load.
const CHART_WINDOW_MS = 5 * 60 * 1000;

export interface SeriesPoint {
  t: number;
  v: number | null;
}

interface History {
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
 * Poll the control API on an interval while `active`, exposing the latest
 * fleet snapshot plus a rolling per-sensor history for the charts.
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
  const historyRef = useRef<Map<string, History>>(new Map());

  const accumulate = useCallback((snap: FleetSnapshot) => {
    const now = Date.now();
    const cutoff = now - CHART_WINDOW_MS;
    for (const m of snap.machines) {
      let h = historyRef.current.get(m.machine_id);
      if (!h) {
        h = { t: [], s: {} };
        historyRef.current.set(m.machine_id, h);
      }
      const sample = m.last_sample ?? {};
      const names = m.sensors?.length ? m.sensors : Object.keys(sample);
      h.t.push(now);
      for (const n of names) {
        if (!h.s[n]) h.s[n] = [];
        const v = sample[n];
        h.s[n].push(v === undefined || v === null ? null : Number(v));
      }
      // Drop samples older than the window.
      let drop = 0;
      while (drop < h.t.length && h.t[drop] < cutoff) drop++;
      if (drop > 0) {
        h.t.splice(0, drop);
        for (const key of Object.keys(h.s)) h.s[key].splice(0, drop);
      }
    }
  }, []);

  useEffect(() => {
    if (!active || !client) {
      setStatus(active ? "unknown" : "standby");
      return;
    }

    let cancelled = false;
    setStatus("connecting");

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
        if (chartsOn) accumulate(snap);
        setSnapshot(snap);
        setStatus("online");
        setLastUpdated(Date.now());
        setOfflineDetail("");
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
  }, [client, active, chartsOn, accumulate]);

  // Clear accumulated history when charts are switched off.
  useEffect(() => {
    if (!chartsOn) historyRef.current.clear();
  }, [chartsOn]);

  const getSeries = useCallback((machineId: string, sensor: string): SeriesPoint[] => {
    const h = historyRef.current.get(machineId);
    if (!h) return [];
    const arr = h.s[sensor];
    if (!arr) return [];
    return h.t.map((t, i) => ({ t, v: arr[i] ?? null }));
  }, []);

  return { snapshot, status, lastUpdated, offlineDetail, getSeries };
}
