/**
 * Evidence payload readers.
 *
 * `normalized_data` is a union of shapes, one per query template. Nothing in
 * the payload says which one it is, so the readers below sniff the shape and
 * return null when they don't recognise it — never a guessed structure.
 *
 * Every reader is total: unknown payload → null → the UI shows "无法解析",
 * not a crash and not an empty table that looks like "no data".
 */
import type { ChartPoint } from "../ui/charts";

type Dict = Record<string, unknown>;

const isDict = (v: unknown): v is Dict =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

/* ── metrics ─────────────────────────────────────────────────────────────── */

export interface MetricSeries {
  service: string | null;
  points: ChartPoint[];
  totalRecords: number | null;
  truncated: boolean;
}

/**
 * A Prometheus-style matrix: `{ result_type: "matrix", series: [{ metric, values }] }`.
 * `values` rows are `[unix_seconds, value]`.
 */
export function metricSeriesOf(payload: unknown): MetricSeries[] {
  if (!isDict(payload)) return [];
  const series = payload.series;
  if (!Array.isArray(series)) return [];
  const out: MetricSeries[] = [];
  for (const s of series) {
    if (!isDict(s)) continue;
    const metric = isDict(s.metric) ? s.metric : null;
    const service = metric ? (str(metric.service) ?? str(metric.__name__)) : null;
    const values = Array.isArray(s.values) ? s.values : [];
    const points: ChartPoint[] = [];
    for (const row of values) {
      if (!Array.isArray(row) || row.length < 2) continue;
      const t = num(row[0]);
      const v = num(row[1]);
      if (t === null || v === null) continue;
      points.push({ t, v });
    }
    points.sort((a, b) => a.t - b.t);
    out.push({
      service,
      points,
      totalRecords: num(payload.total_records),
      truncated: payload.truncated === true,
    });
  }
  return out;
}

/* ── logs ────────────────────────────────────────────────────────────────── */

export interface LogRecord {
  timestamp: string | null;
  message: string | null;
  level: string | null;
  traceId: string | null;
  spanId: string | null;
  labels: Record<string, string>;
}

/** `{ kind: "logs", records: [{ timestamp, message, labels, trace_id, span_id }] }` */
export function logRecordsOf(payload: unknown): LogRecord[] {
  if (!isDict(payload)) return [];
  const records = payload.records;
  if (!Array.isArray(records)) return [];
  const out: LogRecord[] = [];
  for (const r of records) {
    if (!isDict(r)) continue;
    const labels: Record<string, string> = {};
    if (isDict(r.labels)) {
      for (const [k, v] of Object.entries(r.labels)) {
        const s = str(v);
        if (s !== null) labels[k] = s;
      }
    }
    out.push({
      timestamp: str(r.timestamp) ?? str(r.time) ?? str(r.at),
      message: str(r.message) ?? str(r.msg),
      level: str(r.level) ?? str(r.severity),
      traceId: str(r.trace_id) ?? str(r.traceId),
      spanId: str(r.span_id) ?? str(r.spanId),
      labels,
    });
  }
  return out;
}

/* ── traces ──────────────────────────────────────────────────────────────── */

export interface SpanRecord {
  traceId: string | null;
  spanId: string | null;
  service: string | null;
  operation: string | null;
  startTime: string | null;
  durationUs: number | null;
  error: boolean | string | null;
}

/** `{ kind: "traces", spans: [{ trace_id, span_id, service, operation_name, ... }] }` */
export function spanRecordsOf(payload: unknown): SpanRecord[] {
  if (!isDict(payload)) return [];
  const spans = payload.spans;
  if (!Array.isArray(spans)) return [];
  const out: SpanRecord[] = [];
  for (const s of spans) {
    if (!isDict(s)) continue;
    const startMicros = num(s.start_time);
    const err = s.error;
    out.push({
      traceId: str(s.trace_id) ?? str(s.traceId),
      spanId: str(s.span_id) ?? str(s.spanId),
      service: str(s.service),
      operation: str(s.operation_name) ?? str(s.operation),
      // The pipeline emits microseconds; normalise to an ISO instant.
      startTime:
        startMicros !== null
          ? new Date(startMicros / 1000).toISOString()
          : (str(s.start_time) ?? str(s.startTime)),
      durationUs: num(s.duration_us) ?? num(s.durationUs),
      error: typeof err === "boolean" ? err : str(err),
    });
  }
  return out;
}

/* ── events ──────────────────────────────────────────────────────────────── */

export interface EventRecord {
  changedAt: string | null;
  changeKind: string | null;
  service: string | null;
  revision: string | null;
  summary: string | null;
  type: string | null;
}

/** `{ kind: "events", events: [{ event: { changed_at, change_kind, service, ... } }] }` */
export function eventRecordsOf(payload: unknown): EventRecord[] {
  if (!isDict(payload)) return [];
  const events = payload.events;
  if (!Array.isArray(events)) return [];
  const out: EventRecord[] = [];
  for (const e of events) {
    if (!isDict(e)) continue;
    const inner = isDict(e.event) ? e.event : e;
    out.push({
      changedAt: str(inner.changed_at) ?? str(inner.changedAt),
      changeKind: str(inner.change_kind) ?? str(inner.changeKind),
      service: str(inner.service),
      revision: str(inner.revision),
      summary: str(inner.summary),
      type: str(inner.type),
    });
  }
  return out;
}

/* ── shape detection ─────────────────────────────────────────────────────── */

export type PayloadShape = "matrix" | "logs" | "traces" | "events" | "unknown";

export function payloadShapeOf(payload: unknown): PayloadShape {
  if (!isDict(payload)) return "unknown";
  const kind = str(payload.kind)?.toLowerCase() ?? "";
  if (payload.result_type === "matrix" || kind === "matrix") return "matrix";
  if (Array.isArray(payload.records)) return "logs";
  if (Array.isArray(payload.spans)) return "traces";
  if (Array.isArray(payload.events)) return "events";
  if (kind) return "unknown";
  return "unknown";
}

/** Total record count reported by the payload, when it declares one. */
export function payloadTotal(payload: unknown): number | null {
  if (!isDict(payload)) return null;
  return num(payload.total_records) ?? num(payload.totalRecords);
}
