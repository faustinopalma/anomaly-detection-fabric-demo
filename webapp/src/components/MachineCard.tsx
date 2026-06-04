import { useEffect, useState } from "react";

import { SensorChart } from "./SensorChart";
import { useToast } from "./Toasts";
import { ApiClient, readError } from "../api/client";
import { colorForSensor } from "../theme/palette";
import type { DetectionMarker, InjectionBand, SeriesPoint } from "../hooks/useFleet";
import type { InjectKind, Machine } from "../types";

const INJECT_KINDS: InjectKind[] = ["spike", "drift", "stuck"];

interface Props {
  machine: Machine;
  client: ApiClient;
  chartsOn: boolean;
  getSeries: (machineId: string, sensor: string) => SeriesPoint[];
  getInjections: (machineId: string, sensor: string) => InjectionBand[];
  getDetections: (machineId: string, sensor: string) => DetectionMarker[];
}

export function MachineCard({
  machine,
  client,
  chartsOn,
  getSeries,
  getInjections,
  getDetections,
}: Props) {
  const { push: toast } = useToast();
  const id = machine.machine_id;

  const [randomChecked, setRandomChecked] = useState(machine.random_enabled);
  const [randomBusy, setRandomBusy] = useState(false);
  const [forcedValue, setForcedValue] = useState(machine.forced_state ?? "");
  const [stateBusy, setStateBusy] = useState(false);

  // Reconcile local control widgets with the server snapshot, unless the user
  // just changed them (a request is in flight) — avoids UI flicker.
  useEffect(() => {
    if (!randomBusy) setRandomChecked(machine.random_enabled);
  }, [machine.random_enabled, randomBusy]);
  useEffect(() => {
    if (!stateBusy) setForcedValue(machine.forced_state ?? "");
  }, [machine.forced_state, stateBusy]);

  const sample = machine.last_sample ?? {};
  const sensors = machine.sensors?.length ? machine.sensors : Object.keys(sample);
  const validStates = machine.valid_states ?? [];

  const badgeText = machine.active_anomaly
    ? `${machine.state} · ${machine.active_anomaly}`
    : machine.state;
  const badgeClass = machine.active_anomaly
    ? "state-anom"
    : machine.active
      ? "state-on"
      : "state-off";

  async function onToggleRandom(next: boolean) {
    setRandomChecked(next);
    setRandomBusy(true);
    try {
      const res = await client.setRandom(id, next);
      if (!res.ok) throw new Error(await readError(res));
      toast(`${id}: random anomalies ${next ? "ON" : "OFF"}`, "ok");
    } catch (err) {
      setRandomChecked(!next);
      toast(`${id}: failed to set random (${(err as Error).message})`, "err");
    } finally {
      setTimeout(() => setRandomBusy(false), 500);
    }
  }

  async function onInject(kind: InjectKind) {
    try {
      const res = await client.inject(id, kind);
      if (!res.ok) throw new Error(await readError(res));
      toast(`${id}: injected ${kind}`, "ok");
    } catch (err) {
      toast(`${id}: inject ${kind} failed (${(err as Error).message})`, "err");
    }
  }

  async function onForceState(next: string) {
    setForcedValue(next);
    setStateBusy(true);
    try {
      const res = await client.forceState(id, next || null);
      if (!res.ok) throw new Error(await readError(res));
      toast(`${id}: state ${next ? "forced to " + next : "back to auto"}`, "ok");
    } catch (err) {
      toast(`${id}: failed to set state (${(err as Error).message})`, "err");
    } finally {
      setTimeout(() => setStateBusy(false), 800);
    }
  }

  return (
    <div className={`machine${machine.active ? "" : " inactive"}`}>
      <div className="machine-head">
        <span className="id">{id}</span>
        <span className={`state-badge ${badgeClass}`}>{badgeText}</span>
      </div>

      {validStates.length > 0 && (
        <div className="row">
          <span className="label">Force state</span>
          <select
            className="force-state"
            value={forcedValue}
            onChange={(e) => void onForceState(e.target.value)}
          >
            <option value="">Auto (FSM)</option>
            {validStates.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="row">
        <span className="label">Random anomalies</span>
        <label className="switch">
          <input
            type="checkbox"
            checked={randomChecked}
            onChange={(e) => void onToggleRandom(e.target.checked)}
          />
          <span className="slider" />
        </label>
      </div>

      <div className="row">
        <span className="label">Inject manually</span>
      </div>
      <div className="inject-btns">
        {INJECT_KINDS.map((kind) => (
          <button key={kind} type="button" onClick={() => void onInject(kind)}>
            {kind[0].toUpperCase() + kind.slice(1)}
          </button>
        ))}
      </div>

      <div className="sensors">
        {sensors.map((n) => {
          const v = sample[n];
          const txt = v === undefined || v === null ? "\u2014" : Number(v).toFixed(2);
          return (
            <div className="sensor-row" key={n}>
              <span className="sname">{n}</span>
              <span className="sval">{txt}</span>
            </div>
          );
        })}
      </div>

      {chartsOn && sensors.length > 0 && (
        <div className="charts">
          {sensors.map((n, i) => (
            <SensorChart
              key={n}
              sensor={n}
              color={colorForSensor(i)}
              data={getSeries(id, n)}
              injections={getInjections(id, n)}
              detections={getDetections(id, n)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
