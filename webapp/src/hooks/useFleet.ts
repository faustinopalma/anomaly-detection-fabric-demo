import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api/client";
import type {
  ConnStatus,
  EventsResponse,
  FleetSnapshot,
  HistoryResponse,
} from "../types";

const POLL_MS = 2000;
// Chart window. Must match the server-side retention (history_window_s) so a
// reconnect can backfill the full visible range.
const CHART_WINDOW_MS = 5 * 60 * 1000;
// A Fabric detection is counted as "matched" (true positive) when it falls
// inside an injected window, allowing for a lead before onset and a lag after
// it ends (the model needs to observe a few samples before it reacts).
const MATCH_LEAD_MS = 30 * 1000;
const MATCH_LAG_MS = 120 * 1000;

export interface SeriesPoint {
  t: number;
  v: number | null;
}

// Injection band in client-adjusted ms, for shading the chart of one sensor.
export interface InjectionBand {
  id: number;
  kind: string;
  sensor: string;
  start: number;
  end: number;
  level: number;
  source: string;
}

// Detection marker in client-adjusted ms. `matched` distinguishes a true
// positive (lines up with an injection) from an unmatched detection (the model
// flagged something with no injected ground truth).
export interface DetectionMarker {
  t: number;
  score: number;
  modelName: string;
  sensorId: string | null;
  matched: boolean;
}

interface Ring {
  t: number[];
  s: Record<string, (number | null)[]>;
}

interface MachineEventState {
  injections: InjectionBand[];
  detections: DetectionMarker[];
}

interface FleetState {
  snapshot: FleetSnapshot | null;
  status: ConnStatus;
  lastUpdated: number | null;
  offlineDetail: string;
  getSeries: (machineId: string, sensor: string) => SeriesPoint[];
  getInjections: (machineId: string, sensor: string) => InjectionBand[];
  getDetections: (machineId: string, sensor: string) => DetectionMarker[];
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
  // Per-machine injection bands + Fabric detections, client-adjusted ms.
  const eventsRef = useRef<Map<string, MachineEventState>>(new Map());
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

  const mergeEvents = useCallback((e: EventsResponse) => {
    // Align server timestamps to the local clock, same as the history merge.
    const offset = Date.now() - e.server_time * 1000;
    const cutoff = Date.now() - CHART_WINDOW_MS;
    const next = new Map<string, MachineEventState>();

    for (const m of e.machines) {
      const injections: InjectionBand[] = m.injections
        .map((w) => ({
          id: w.id,
          kind: w.kind,
          sensor: w.sensor,
          start: w.start * 1000 + offset,
          end: w.end * 1000 + offset,
          level: w.level,
          source: w.source,
        }))
        .filter((b) => b.end >= cutoff);
      next.set(m.machine_id, { injections, detections: [] });
    }

    for (const d of e.detections) {
      const t = d.t * 1000 + offset;
      if (t < cutoff) continue;
      let entry = next.get(d.machine_id);
      if (!entry) {
        entry = { injections: [], detections: [] };
        next.set(d.machine_id, entry);
      }
      // A detection is matched when it lines up with any injection window on
      // the same machine (the per-sensor refinement happens at render time for
      // univariate detections). Multivariate detections (sensor_id null) match
      // any window on the machine.
      const matched = entry.injections.some((b) => {
        if (d.sensor_id && b.sensor !== d.sensor_id) return false;
        return t >= b.start - MATCH_LEAD_MS && t <= b.end + MATCH_LAG_MS;
      });
      entry.detections.push({
        t,
        score: d.score,
        modelName: d.model_name,
        sensorId: d.sensor_id,
        matched,
      });
    }

    eventsRef.current = next;
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

          const eres = await client.getEvents();
          if (cancelled || !eres.ok) return;
          const evs = (await eres.json()) as EventsResponse;
          if (cancelled) return;
          mergeEvents(evs);
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
  }, [client, active, chartsOn, mergeHistory, mergeEvents]);

  // Clear accumulated history when charts are switched off.
  useEffect(() => {
    if (!chartsOn) {
      ringRef.current.clear();
      eventsRef.current.clear();
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

  // Injection bands to shade on the charts. Bands are shown machine-wide (on
  // every sensor chart of the machine), not just on the affected sensor, so the
  // injected period is always visible — including rare random injections that
  // hit an arbitrary sensor you may not be looking at.
  const getInjections = useCallback(
    (machineId: string, _sensor: string): InjectionBand[] => {
      const entry = eventsRef.current.get(machineId);
      if (!entry) return [];
      return entry.injections;
    },
    [],
  );

  // Detection markers to draw on a specific sensor chart. Univariate
  // detections (sensor_id set) only render on their own sensor; multivariate
  // detections (sensor_id null) render on every chart of the machine.
  const getDetections = useCallback(
    (machineId: string, sensor: string): DetectionMarker[] => {
      const entry = eventsRef.current.get(machineId);
      if (!entry) return [];
      return entry.detections.filter(
        (d) => d.sensorId == null || d.sensorId === sensor,
      );
    },
    [],
  );

  return {
    snapshot,
    status,
    lastUpdated,
    offlineDetail,
    getSeries,
    getInjections,
    getDetections,
  };
}
