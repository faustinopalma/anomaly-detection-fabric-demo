import type { AccountInfo } from "@azure/msal-browser";

import { useTheme } from "../theme/ThemeProvider";
import type { ConnStatus } from "../types";

const STATUS_META: Record<ConnStatus, { cls: string; label: string }> = {
  unknown: { cls: "pill-unknown", label: "connecting…" },
  connecting: { cls: "pill-unknown", label: "connecting…" },
  online: { cls: "pill-online", label: "online" },
  offline: { cls: "pill-offline", label: "offline" },
  standby: { cls: "pill-unknown", label: "standby" },
};

interface Props {
  account: AccountInfo | null;
  status: ConnStatus;
  paused: boolean;
  chartsOn: boolean;
  onToggleCharts: () => void;
  onTogglePause: () => void;
  onSignOut: () => void;
}

export function Header({
  account,
  status,
  paused,
  chartsOn,
  onToggleCharts,
  onTogglePause,
  onSignOut,
}: Props) {
  const { theme, toggle: toggleTheme } = useTheme();
  const pill = STATUS_META[status];

  return (
    <header>
      <h1>Factory simulator — control panel</h1>
      <div className="header-right">
        {account && (
          <span className="user-info">{account.username || account.name || "signed in"}</span>
        )}
        <button
          type="button"
          className="ghost"
          title="Toggle light/dark theme"
          onClick={toggleTheme}
        >
          {theme === "light" ? "Dark" : "Light"}
        </button>
        {account && (
          <>
            <button
              type="button"
              className={`ghost${chartsOn ? " active" : ""}`}
              onClick={onToggleCharts}
            >
              Charts: {chartsOn ? "on" : "off"}
            </button>
            <button
              type="button"
              className={`ghost${paused ? " active" : ""}`}
              onClick={onTogglePause}
            >
              {paused ? "Resume" : "Pause"}
            </button>
            <button type="button" className="signout" onClick={onSignOut}>
              Sign out
            </button>
          </>
        )}
        <div className={`pill ${pill.cls}`}>{account ? pill.label : "not connected"}</div>
      </div>
    </header>
  );
}
