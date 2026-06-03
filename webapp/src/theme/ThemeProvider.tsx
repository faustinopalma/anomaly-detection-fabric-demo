import {
  createContext,
  use,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { CHART_PALETTE, type ChartPalette, type ThemeName } from "./palette";

const STORAGE_KEY = "panel-theme";

interface ThemeContextValue {
  theme: ThemeName;
  palette: ChartPalette;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function initialTheme(): ThemeName {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* ignore unavailable storage */
  }
  const prefersLight =
    window.matchMedia?.("(prefers-color-scheme: light)").matches ?? false;
  return prefersLight ? "light" : "dark";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<ThemeName>(initialTheme);

  // Drive the CSS variables off a single attribute on <html>. React owns this,
  // so a theme change re-paints the whole UI and re-renders every chart.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next: ThemeName = prev === "light" ? "dark" : "light";
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, palette: CHART_PALETTE[theme], toggle }),
    [theme, toggle],
  );

  return <ThemeContext value={value}>{children}</ThemeContext>;
}

export function useTheme(): ThemeContextValue {
  const ctx = use(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}
