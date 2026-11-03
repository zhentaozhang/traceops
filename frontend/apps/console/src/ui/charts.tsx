/**
 * Charts.
 *
 * Hand-rolled SVG instead of a charting library: the console draws exactly two
 * shapes (a time series and a comparison bar), and a library would cost more
 * bundle than the whole app. Every value shown here comes from real payloads —
 * no synthetic series, no invented smoothing.
 */
import { useEffect, useRef, useState } from "react";
import { formatInstantShort } from "../domain/format";

export type Tone = "accent" | "supported" | "critical" | "info" | "neutral";

export interface ChartPoint {
  t: number;
  v: number;
}

export interface ChartSeries {
  label: string;
  points: ChartPoint[];
  tone?: Tone;
}

export interface ChartMarker {
  t: number;
  label: string;
}

const TONE_VAR: Record<Tone, string> = {
  accent: "var(--accent)",
  supported: "var(--verdict-supported)",
  critical: "var(--severity-critical)",
  info: "var(--severity-minor)",
  neutral: "var(--text-tertiary)",
};

/** Measure a container so the chart can be drawn in real pixels. */
function useWidth<T extends HTMLElement>(fallback = 640) {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      // Compare against the live value, not the one captured at mount.
      if (w) setWidth((prev) => (Math.abs(w - prev) > 0.5 ? w : prev));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

function niceCeil(v: number): number {
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = 10 ** exp;
  const n = v / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * base;
}

function fmtValue(v: number): string {
  const abs = Math.abs(v);
  if (abs === 0) return "0";
  if (abs < 0.001) return v.toExponential(1);
  if (abs < 1) return v.toFixed(3);
  if (abs < 100) return v.toFixed(2).replace(/\.?0+$/, "");
  return Math.round(v).toLocaleString("en-US");
}

/**
 * A time series with an optional anomaly marker.
 *
 * `t` is a unix timestamp in **seconds** — the shape Prometheus matrices use.
 * Callers convert when they have milliseconds.
 */
export function TimeSeriesChart({
  series,
  markers = [],
  height = 190,
  unit,
  emptyHint = "没有数据点",
}: {
  series: ChartSeries[];
  markers?: ChartMarker[];
  height?: number;
  unit?: string;
  emptyHint?: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();

  const pad = { top: 16, right: 16, bottom: 24, left: 52 };
  const innerW = Math.max(10, width - pad.left - pad.right);
  const innerH = Math.max(10, height - pad.top - pad.bottom);

  const points = series.flatMap((s) => s.points);
  const hasData = points.length > 0;

  const tMin = hasData ? Math.min(...points.map((p) => p.t)) : 0;
  const tMax = hasData ? Math.max(...points.map((p) => p.t)) : 1;
  const vMaxRaw = hasData ? Math.max(...points.map((p) => p.v)) : 1;
  const vMinRaw = hasData ? Math.min(...points.map((p) => p.v)) : 0;
  const vMax = niceCeil(Math.max(vMaxRaw, vMinRaw > 0 ? 0 : vMaxRaw));
  const vMin = Math.min(0, vMinRaw);
  const span = tMax - tMin || 1;

  const x = (t: number) => pad.left + ((t - tMin) / span) * innerW;
  const y = (v: number) => pad.top + innerH - ((v - vMin) / (vMax - vMin || 1)) * innerH;

  const ticks = [vMin, (vMin + vMax) / 2, vMax];

  return (
    <div ref={ref} className="chart">
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={hasData ? "时间序列图" : emptyHint}
        style={{ display: "block" }}
      >
        {!hasData ? (
          <text
            x={width / 2}
            y={height / 2}
            textAnchor="middle"
            fill="var(--text-tertiary)"
            fontSize="11"
            fontFamily="var(--font-mono)"
          >
            {emptyHint}
          </text>
        ) : (
          <>
            {ticks.map((tv, i) => (
              <g key={tv}>
                <line
                  x1={pad.left}
                  x2={pad.left + innerW}
                  y1={y(tv)}
                  y2={y(tv)}
                  stroke="var(--border-subtle)"
                  strokeDasharray={i === 0 ? undefined : "2 4"}
                />
                <text
                  x={pad.left - 8}
                  y={y(tv) + 3}
                  textAnchor="end"
                  fill="var(--text-tertiary)"
                  fontSize="10"
                  fontFamily="var(--font-mono)"
                >
                  {fmtValue(tv)}
                </text>
              </g>
            ))}

            {markers.map((m) => (
              <g key={`${m.t}-${m.label}`}>
                <line
                  x1={x(m.t)}
                  x2={x(m.t)}
                  y1={pad.top}
                  y2={pad.top + innerH}
                  stroke="var(--accent)"
                  strokeDasharray="3 3"
                  opacity={0.7}
                />
                <circle cx={x(m.t)} cy={pad.top} r="3" fill="var(--accent)" />
                <text
                  x={x(m.t)}
                  y={pad.top - 5}
                  textAnchor="middle"
                  fill="var(--accent-strong)"
                  fontSize="9"
                  fontFamily="var(--font-mono)"
                >
                  {m.label}
                </text>
              </g>
            ))}

            {series.map((s, si) => {
              if (s.points.length === 0) return null;
              const tone = TONE_VAR[s.tone ?? "accent"];
              const line = s.points
                .map(
                  (p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(2)},${y(p.v).toFixed(2)}`,
                )
                .join(" ");
              const area = `${line} L${x(s.points[s.points.length - 1]!.t).toFixed(2)},${(pad.top + innerH).toFixed(2)} L${x(s.points[0]!.t).toFixed(2)},${(pad.top + innerH).toFixed(2)} Z`;
              const gid = `g-${si}-${s.label.replace(/\W/g, "")}`;
              return (
                <g key={s.label}>
                  {series.length === 1 && (
                    <>
                      <defs>
                        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor={tone} stopOpacity="0.22" />
                          <stop offset="100%" stopColor={tone} stopOpacity="0" />
                        </linearGradient>
                      </defs>
                      <path d={area} fill={`url(#${gid})`} />
                    </>
                  )}
                  <path
                    d={line}
                    fill="none"
                    stroke={tone}
                    strokeWidth="1.6"
                    strokeLinejoin="round"
                    strokeLinecap="round"
                  />
                  {s.points.length <= 40 &&
                    s.points.map((p) => (
                      <circle
                        key={`${p.t}-${p.v}`}
                        cx={x(p.t)}
                        cy={y(p.v)}
                        r="2"
                        fill="var(--surface-raised)"
                        stroke={tone}
                        strokeWidth="1.2"
                      />
                    ))}
                </g>
              );
            })}

            <text
              x={pad.left}
              y={height - 6}
              fill="var(--text-tertiary)"
              fontSize="10"
              fontFamily="var(--font-mono)"
            >
              {formatInstantShort(new Date(tMin * 1000).toISOString())}
            </text>
            <text
              x={pad.left + innerW}
              y={height - 6}
              textAnchor="end"
              fill="var(--text-tertiary)"
              fontSize="10"
              fontFamily="var(--font-mono)"
            >
              {formatInstantShort(new Date(tMax * 1000).toISOString())}
            </text>
            {unit && (
              <text
                x={pad.left + innerW}
                y={pad.top - 5}
                textAnchor="end"
                fill="var(--text-tertiary)"
                fontSize="9"
                fontFamily="var(--font-mono)"
              >
                {unit}
              </text>
            )}
          </>
        )}
      </svg>
    </div>
  );
}

/**
 * A labelled bar. Used wherever the question is "how much of what" — recall,
 * precision, coverage — never for values that need a shared scale.
 */
export function BarRow({
  label,
  ratio,
  display,
  tone = "accent",
  note,
}: {
  label: string;
  ratio: number | null;
  display?: string;
  tone?: Tone;
  note?: string;
}) {
  const pct = ratio === null ? null : Math.min(100, Math.max(0, ratio * 100));
  return (
    <div className="bar-row">
      <div className="bar-row__label">
        <span>{label}</span>
        {note && <span className="faint">{note}</span>}
      </div>
      <div className="bar-row__track">
        <div
          className="bar-row__fill"
          style={{
            width: pct === null ? "0%" : `${pct}%`,
            background: TONE_VAR[tone],
          }}
        />
      </div>
      <div className="bar-row__value num">
        {display ?? (pct === null ? "—" : `${pct.toFixed(1)}%`)}
      </div>
    </div>
  );
}

/** Two bars side by side: ours vs a naive baseline. */
export function CompareBars({
  ours,
  theirs,
  oursLabel = "本系统",
  theirsLabel = "基线",
}: {
  ours: { label: string; ratio: number | null };
  theirs: { label: string; ratio: number | null };
  oursLabel?: string;
  theirsLabel?: string;
}) {
  return (
    <div className="compare">
      <div className="compare__row">
        <span className="compare__tag compare__tag--ours">{oursLabel}</span>
        <div className="compare__track">
          <div
            className="compare__fill compare__fill--ours"
            style={{ width: `${Math.min(100, (ours.ratio ?? 0) * 100)}%` }}
          />
        </div>
        <span className="compare__value num">
          {ours.ratio === null ? "—" : `${(ours.ratio * 100).toFixed(1)}%`}
        </span>
      </div>
      <div className="compare__row">
        <span className="compare__tag">{theirsLabel}</span>
        <div className="compare__track">
          <div
            className="compare__fill"
            style={{ width: `${Math.min(100, (theirs.ratio ?? 0) * 100)}%` }}
          />
        </div>
        <span className="compare__value num">
          {theirs.ratio === null ? "—" : `${(theirs.ratio * 100).toFixed(1)}%`}
        </span>
      </div>
    </div>
  );
}
