export type ThemeName = "light" | "dark";

// Series colors for the per-sensor charts (shared by both themes; tuned to read
// well on either background).
export const SERIES_COLORS = [
  "#4f9cf9",
  "#2ea043",
  "#d29922",
  "#f85149",
  "#a371f7",
  "#3fb6b2",
  "#db61a2",
  "#e3b341",
] as const;

// Axis / grid / tooltip colors per theme, passed explicitly to Recharts so the
// charts re-render with the right contrast when the theme flips.
export interface ChartPalette {
  axis: string;
  grid: string;
  text: string;
  cursor: string;
  tooltipBg: string;
  tooltipBorder: string;
}

export const CHART_PALETTE: Record<ThemeName, ChartPalette> = {
  dark: {
    axis: "#3a4654",
    grid: "#222c39",
    text: "#8b97a7",
    cursor: "#3a4654",
    tooltipBg: "#1a212b",
    tooltipBorder: "#2e3a48",
  },
  light: {
    axis: "#c8d1da",
    grid: "#e7ebf0",
    text: "#59636e",
    cursor: "#c8d1da",
    tooltipBg: "#ffffff",
    tooltipBorder: "#d0d7de",
  },
};

export function colorForSensor(index: number): string {
  return SERIES_COLORS[index % SERIES_COLORS.length];
}
