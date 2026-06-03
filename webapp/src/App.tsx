import { useCallback, useEffect, useMemo, useState } from "react";
import type { AccountInfo } from "@azure/msal-browser";

import { Header } from "./components/Header";
import { MachineCard } from "./components/MachineCard";
import { ApiClient } from "./api/client";
import { authConfigured } from "./config";
import { initAuth, signIn, signOut } from "./auth/msal";
import { useFleet } from "./hooks/useFleet";

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

  const { snapshot, status, lastUpdated, offlineDetail, getSeries } = useFleet(
    client,
    active,
    chartsOn,
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
          <div className="grid">
            {machines.map((m) => (
              <MachineCard
                key={m.machine_id}
                machine={m}
                client={client}
                chartsOn={chartsOn}
                getSeries={getSeries}
              />
            ))}
          </div>
        )}
      </main>

      <footer>
        <span>
          Polls <code>/api/state</code> every 2&nbsp;s. Suggested demo flow: turn a machine's
          random anomalies <em>off</em>, then inject a spike, drift or stuck anomaly manually to
          show the detector reacting.
        </span>
      </footer>
    </>
  );
}
