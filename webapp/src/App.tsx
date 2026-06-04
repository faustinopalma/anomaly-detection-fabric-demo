import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AccountInfo } from "@azure/msal-browser";

import { Header } from "./components/Header";
import { MachineCard } from "./components/MachineCard";
import { ApiClient } from "./api/client";
import { authConfigured } from "./config";
import { initAuth, signIn, signOut } from "./auth/msal";
import { useFleet } from "./hooks/useFleet";
import type { AnomalyLevel } from "./types";

const LEVELS: AnomalyLevel[] = [1, 2, 3, 4, 5];
const LEVEL_LABELS: Record<AnomalyLevel, string> = {
  1: "Lieve",
  2: "Bassa",
  3: "Media",
  4: "Alta",
  5: "Severa",
};

function fmtDuration(s: number): string {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

export function App() {
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [hiddenPause, setHiddenPause] = useState(false);
  const [chartsOn, setChartsOn] = useState(true);
  // Global anomaly-strength level (1..5), applied to every machine. Seeded
  // once from the server snapshot, then driven by the operator.
  const [level, setLevelState] = useState<AnomalyLevel>(3);
  const levelSeeded = useRef(false);

  // Boot: initialize MSAL and resolve any existing session / redirect result.
  useEffect(() => {
    let cancelled = false;
    initAuth()
      .then((acc) => {
        if (cancelled) return;
        setAccount(acc);
        setAuthReady(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setAuthError(`Sign-in failed: ${(err as Error).message}`);
        setAuthReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Auto-standby while the tab is hidden (stops polling, zero requests).
  useEffect(() => {
    const onVisibility = () => setHiddenPause(document.hidden);
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  const client = useMemo(() => (account ? new ApiClient(account) : null), [account]);
  const active = Boolean(account) && !paused && !hiddenPause;

  const { snapshot, status, lastUpdated, offlineDetail, getSeries, getInjections, getDetections } =
    useFleet(client, active, chartsOn);

  // Seed the level selector from the server once, so a refresh reflects the
  // live value without overriding subsequent operator changes.
  useEffect(() => {
    if (!levelSeeded.current && snapshot?.level != null) {
      const lvl = Math.max(1, Math.min(5, snapshot.level)) as AnomalyLevel;
      setLevelState(lvl);
      levelSeeded.current = true;
    }
  }, [snapshot?.level]);

  const onSetLevel = useCallback(
    (lvl: AnomalyLevel) => {
      setLevelState(lvl);
      levelSeeded.current = true;
      if (client) void client.setLevel(lvl);
    },
    [client],
  );

  const onSignOut = useCallback(() => {
    if (account) signOut(account);
  }, [account]);

  const machines = snapshot?.machines ?? [];

  return (
    <>
      <Header
        account={account}
        status={paused ? "standby" : status}
        paused={paused}
        chartsOn={chartsOn}
        onToggleCharts={() => setChartsOn((v) => !v)}
        onTogglePause={() => setPaused((v) => !v)}
        onSignOut={onSignOut}
      />

      <main className="content">
        {!account && authReady && (
          <section className="card">
            <h2>Sign in required</h2>
            <p className="hint">
              Access is restricted to authorized users in the organization tenant. Sign in
              with your work account to control the simulator.
            </p>
            <button
              type="button"
              className="primary"
              disabled={!authConfigured}
              onClick={() => void signIn()}
            >
              Sign in with Microsoft
            </button>
            {!authConfigured && (
              <p className="error">
                Runtime config failed to load (clientId/tenantId missing). Try a hard refresh.
              </p>
            )}
            {authError && <p className="error">{authError}</p>}
          </section>
        )}

        {account && status === "offline" && (
          <section className="banner">
            <strong>Container unreachable.</strong> <span>{offlineDetail}</span>
          </section>
        )}

        {account && snapshot && status === "online" && (
          <div className="meta">
            <span>{snapshot.machine_count} machine(s)</span>
            <span>uptime {fmtDuration(snapshot.uptime_s)}</span>
            {lastUpdated && <span>updated {new Date(lastUpdated).toLocaleTimeString()}</span>}
          </div>
        )}

        {account && client && (
          <div className="toolbar">
            <span className="toolbar-label">Forza anomalie</span>
            <div className="level-picker" role="group" aria-label="Livello forza anomalie">
              {LEVELS.map((lvl) => (
                <button
                  key={lvl}
                  type="button"
                  className={`level-btn${lvl === level ? " active" : ""}`}
                  aria-pressed={lvl === level}
                  title={`Livello ${lvl} — ${LEVEL_LABELS[lvl]}`}
                  onClick={() => onSetLevel(lvl)}
                >
                  {lvl}
                </button>
              ))}
            </div>
            <span className="toolbar-hint">
              {LEVEL_LABELS[level]} · vale per tutte le macchine
            </span>
          </div>
        )}

        {account && client && (
          <div className="grid">
            {machines.map((m) => (
              <MachineCard
                key={m.machine_id}
                machine={m}
                client={client}
                chartsOn={chartsOn}
                getSeries={getSeries}
                getInjections={getInjections}
                getDetections={getDetections}
              />
            ))}
          </div>
        )}
      </main>

      <footer>
        <span>
          Polls <code>/api/state</code> every 2&nbsp;s. Imposta la <em>forza</em> (1–5), poi
          inietta spike, drift o stuck: la <strong>banda ambra</strong> sul grafico segna il
          periodo iniettato, una <strong>linea verde</strong> quando Fabric rileva l'anomalia
          corrispondente e una <strong>linea rossa tratteggiata</strong> per le rilevazioni
          non corrisposte.
        </span>
      </footer>
    </>
  );
}
