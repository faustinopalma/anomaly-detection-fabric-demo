import { memo, useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useTheme } from "../theme/ThemeProvider";
import type { SeriesPoint } from "../hooks/useFleet";

const CHART_HEIGHT = 132;
const WINDOW_MS = 5 * 60 * 1000;

function fmtTime(t: number): string {
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtValue(v: number): string {
  const abs = Math.abs(v);
  if (abs !== 0 && (abs >= 10000 || abs < 0.01)) return v.toExponential(1);
  return v.toFixed(abs >= 100 ? 0 : 2);
}

interface Props {
  sensor: string;
  color: string;
  data: SeriesPoint[];
}

/**
 * One fixed-height line chart for a single sensor. The X axis is a fixed
 * 5-minute window (newest sample at the right edge, scrolling left). The Y
 * axis auto-scales to the data but always keeps zero in view, even when the
 * signal sits far from zero.
 */
function SensorChartImpl({ sensor, color, data }: Props) {
  const { palette } = useTheme();

  const { tLeft, tRight, yDomain } = useMemo(() => {
    const now = Date.now();
    let lo = Infinity;
    let hi = -Infinity;
    for (const p of data) {
      if (p.v == null) continue;
      if (p.v < lo) lo = p.v;
      if (p.v > hi) hi = p.v;
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
      lo = 0;
      hi = 1;
    }
    // Always include zero in the visible range.
    let yMin = Math.min(0, lo);
    let yMax = Math.max(0, hi);
    if (yMax <= yMin) yMax = yMin + 1;
    const headroom = (yMax - yMin) * 0.08;
    yMax += headroom;
    if (yMin < 0) yMin -= headroom;
    return {
      tLeft: now - WINDOW_MS,
      tRight: now,
      yDomain: [yMin, yMax] as [number, number],
    };
  }, [data]);

  return (
    <div className="sensor-chart">
      <span className="sensor-chart-title">
        <i className="series-dot" style={{ background: color }} />
        {sensor}
      </span>
      <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
        <LineChart data={data} margin={{ top: 6, right: 10, bottom: 2, left: 4 }}>
          <CartesianGrid stroke={palette.grid} strokeDasharray="3 3" />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={[tLeft, tRight]}
            tickFormatter={fmtTime}
            tickCount={5}
            stroke={palette.axis}
            tick={{ fill: palette.text, fontSize: 10 }}
            minTickGap={28}
          />
          <YAxis
            type="number"
            domain={yDomain}
            tickFormatter={fmtValue}
            width={46}
            stroke={palette.axis}
            tick={{ fill: palette.text, fontSize: 10 }}
          />
          <Tooltip
            isAnimationActive={false}
            labelFormatter={(t) => fmtTime(Number(t))}
            formatter={(v) => [fmtValue(Number(v)), sensor]}
            cursor={{ stroke: palette.cursor }}
            contentStyle={{
              background: palette.tooltipBg,
              border: `1px solid ${palette.tooltipBorder}`,
              borderRadius: 8,
              fontSize: 12,
            }}
            labelStyle={{ color: palette.text }}
          />
          <Line
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export const SensorChart = memo(SensorChartImpl);
