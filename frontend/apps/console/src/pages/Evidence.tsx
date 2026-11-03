/**
 * Evidence Explorer.
 *
 * Five tabs, one per evidence kind. The point of the page is that every claim
 * the investigator makes must be traceable back to a query that was actually
 * run, over a window, with a checksum — so the payload is always one click
 * away from the conclusion that cites it.
 */
import { useEffect, useMemo, useState } from "react";
import { External, X } from "../app/icons";
import { useAnnotations } from "../data/annotations";
import { dataSource, type EvidenceDetailVM } from "../data/source";
import {
  eventRecordsOf,
  logRecordsOf,
  metricSeriesOf,
  payloadShapeOf,
  payloadTotal,
  spanRecordsOf,
} from "../domain/evidence-view";
import {
  evidenceTypeLabel,
  formatInstant,
  formatInstantShort,
  formatPercent,
} from "../domain/format";
import { TimeSeriesChart } from "../ui/charts";
import {
  Badge,
  EmptyState,
  ErrorState,
  Hash,
  Ident,
  JsonViewer,
  KeyValue,
  Panel,
  Stat,
} from "../ui/primitives";

const TABS = ["metric", "log", "trace", "event", "relation"] as const;
type Tab = (typeof TABS)[number];

const TAB_LABELS: Record<Tab, string> = {
  metric: "指标",
  log: "日志",
  trace: "追踪",
  event: "事件",
  relation: "关系",
};

export function EvidencePage() {
  const [runs, setRuns] = useState<string[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<EvidenceDetailVM[]>([]);
  const [tab, setTab] = useState<Tab>("metric");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const { items: annotations } = useAnnotations(runId);

  useEffect(() => {
    let alive = true;
    void dataSource.listIncidents().then((incidents) => {
      if (!alive) return;
      const ids = [
        ...new Set(
          incidents
            .map((i) => i.currentRunId)
            .filter((id): id is string => typeof id === "string" && id.length > 0),
        ),
      ].sort();
      setRuns(ids);
      setRunId((prev) => prev ?? ids[0] ?? null);
    });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!runId) {
      setEvidence([]);
      return;
    }
    let alive = true;
    setFailed(false);
    void dataSource
      .getEvidence(runId)
      .then((list) => {
        if (!alive) return;
        setEvidence(list);
        setSelectedId(null);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [runId]);

  const byType = useMemo(() => {
    const map = new Map<Tab, EvidenceDetailVM[]>();
    for (const t of TABS) map.set(t, []);
    for (const e of evidence) {
      const key = (TABS as readonly string[]).includes(e.evidenceType)
        ? (e.evidenceType as Tab)
        : "metric";
      map.get(key)?.push(e);
    }
    return map;
  }, [evidence]);

  const selected = useMemo(
    () => evidence.find((e) => e.id === selectedId) ?? null,
    [evidence, selectedId],
  );

  const counts = useMemo(
    () => ({
      total: evidence.length,
      annotated: annotations.length,
    }),
    [evidence.length, annotations.length],
  );

  return (
    <>
      <div className="page-head">
        <div className="page-head__text">
          <div className="eyebrow">
            <span className="label">证据浏览器</span>
          </div>
          <h1 className="page-title">每条结论都能落到原始数据上</h1>
          <p className="page-sub">
            证据按来源分面：指标、日志、追踪、事件，以及它们之间的关系。
            每条证据都带着它的查询模板、观测窗口和 checksum——结论可以被复核， 而不只是被相信。
          </p>
        </div>
        <div className="page-actions">
          <label className="select-label">
            <span className="label">运行</span>
            <select
              className="select"
              name="run"
              value={runId ?? ""}
              onChange={(e) => setRunId(e.target.value || null)}
              aria-label="选择调查运行"
            >
              {runs.length === 0 && <option value="">没有可用运行</option>}
              {runs.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div className="stat-row">
        <Stat label="证据条目" value={counts.total} hint="本次运行采集到的全部证据" />
        <Stat
          label="人工标注"
          value={counts.annotated}
          hint="保存在本机浏览器，不是权威记录"
          tone={counts.annotated > 0 ? "accent" : "neutral"}
        />
        <Stat
          label="运行"
          value={runId ? <Ident className="mono">{runId}</Ident> : "—"}
          hint="证据归属于一次调查运行"
        />
      </div>

      {/* The list loads asynchronously; without a live region the result
          arrives silently and a screen-reader user has no idea anything
          changed. */}
      <p className="sr-only" role="status" aria-live="polite">
        {failed
          ? "证据载入失败"
          : `已载入 ${counts.total} 条证据，${counts.annotated} 条人工标注`}
      </p>

      <div className="section">
        {/* A real tab pattern: one tab stop for the whole strip, arrow keys
            move between tabs, and the panel is wired to the selected tab via
            aria-controls / aria-labelledby. Without this, a screen-reader
            user hears five unrelated buttons and no idea which pane the
            content below belongs to. */}
        <div className="tabs" role="tablist" aria-label="证据类型">
          {TABS.map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              id={`evidence-tab-${t}`}
              aria-selected={tab === t}
              aria-controls="evidence-panel"
              tabIndex={tab === t ? 0 : -1}
              className={`tab${tab === t ? " is-active" : ""}`}
              onClick={() => setTab(t)}
              onKeyDown={(e) => {
                if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
                e.preventDefault();
                const delta = e.key === "ArrowRight" ? 1 : -1;
                const index = (TABS.indexOf(t) + delta + TABS.length) % TABS.length;
                const next = TABS[index] ?? t;
                setTab(next);
                // Move focus with the selection — otherwise the arrow key
                // changes the tab but the focus ring stays behind.
                document.getElementById(`evidence-tab-${next}`)?.focus();
              }}
            >
              {TAB_LABELS[t]}
              <span className="tab__count">
                {t === "relation" ? annotations.length : (byType.get(t)?.length ?? 0)}
              </span>
            </button>
          ))}
        </div>

        <div
          id="evidence-panel"
          role="tabpanel"
          aria-labelledby={`evidence-tab-${tab}`}
          style={{ marginTop: "var(--space-5)" }}
        >
          {failed ? (
            <Panel>
              <ErrorState />
            </Panel>
          ) : tab === "relation" ? (
            <RelationsView runId={runId} evidence={evidence} annotations={annotations} />
          ) : (
            <div className="split">
              <Panel
                className="panel--flush"
                title="证据列表"
                meta={`${byType.get(tab)?.length ?? 0} 条`}
              >
                {(byType.get(tab)?.length ?? 0) === 0 ? (
                  <EmptyState title="没有这类证据" detail="这次运行没有采集到该类型的证据。" />
                ) : (
                  (byType.get(tab) ?? []).map((e) => (
                    <button
                      key={e.id}
                      type="button"
                      className="ev"
                      onClick={() => setSelectedId(e.id)}
                      style={
                        selectedId === e.id ? { background: "var(--accent-tint)" } : undefined
                      }
                    >
                      <span className="ev__icon">
                        {evidenceTypeLabel(e.evidenceType).slice(0, 2)}
                      </span>
                      <span style={{ minWidth: 0 }}>
                        <span className="ev__title">
                          {e.serviceId ?? "全局"} · {e.queryTemplateId ?? "—"}
                        </span>
                        <span className="ev__meta">
                          <span>{formatInstantShort(e.observedAt)}</span>
                          <Hash value={e.checksum} />
                        </span>
                      </span>
                      <span className="ev__right">
                        <span>{payloadTotal(e.normalizedData) ?? "—"} 条</span>
                      </span>
                    </button>
                  ))
                )}
              </Panel>

              <EvidenceDetail evidence={selected} />
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ── payload rendering ───────────────────────────────────────────────────── */

function EvidenceDetail({ evidence }: { evidence: EvidenceDetailVM | null }) {
  if (!evidence) {
    return (
      <Panel>
        <EmptyState
          title="选一条证据"
          detail="左侧任意一条证据都会在这里展开：查询、窗口、归一化数据。"
        />
      </Panel>
    );
  }

  const shape = payloadShapeOf(evidence.normalizedData);

  return (
    <div className="stack">
      <Panel title="来源">
        <KeyValue
          items={[
            { label: "证据 ID", value: <Ident className="mono">{evidence.id}</Ident> },
            { label: "类型", value: evidenceTypeLabel(evidence.evidenceType) },
            {
              label: "服务",
              value: <Ident className="mono">{evidence.serviceId ?? "—"}</Ident>,
            },
            { label: "观测时间", value: formatInstant(evidence.observedAt) },
            {
              label: "窗口",
              value: (
                <span className="mono">
                  {formatInstantShort(evidence.windowStart)} →{" "}
                  {formatInstantShort(evidence.windowEnd)}
                </span>
              ),
            },
            {
              label: "查询模板",
              value: <Ident className="mono">{evidence.queryTemplateId ?? "—"}</Ident>,
            },
            { label: "checksum", value: <Hash value={evidence.checksum} /> },
          ]}
        />
        {evidence.sourceLink && (
          <a
            className="btn btn--default btn--sm"
            href={evidence.sourceLink}
            target="_blank"
            rel="noreferrer"
            style={{ marginTop: "var(--space-4)", width: "fit-content" }}
          >
            <External size={13} />
            打开源链接
          </a>
        )}
      </Panel>

      {evidence.renderedQuery && (
        <Panel title="实际查询">
          <pre className="json">{evidence.renderedQuery}</pre>
        </Panel>
      )}

      <Panel title="归一化数据" meta={shape === "unknown" ? "未识别的结构" : undefined}>
        <PayloadView evidence={evidence} />
      </Panel>

      <Panel title="原始负载">
        <JsonViewer value={evidence.rawPayload} maxHeight={300} />
      </Panel>
    </div>
  );
}

function PayloadView({ evidence }: { evidence: EvidenceDetailVM }) {
  const payload = evidence.normalizedData;
  const shape = payloadShapeOf(payload);

  if (shape === "matrix") {
    const series = metricSeriesOf(payload);
    if (series.length === 0) {
      return <EmptyState title="矩阵为空" detail="series 数组里没有可绘制的序列。" />;
    }
    return (
      <div className="stack">
        {series.map((s) => (
          <div key={s.service ?? "series"}>
            <div className="row" style={{ marginBottom: "var(--space-2)" }}>
              <span className="mono" style={{ color: "var(--text-primary)" }}>
                {s.service ?? "series"}
              </span>
              <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
                {s.points.length} 个点
                {s.truncated ? " · 已截断" : ""}
              </span>
            </div>
            <TimeSeriesChart
              series={[
                {
                  label: s.service ?? "series",
                  points: s.points,
                  tone: s === series[0] ? "critical" : "accent",
                },
              ]}
              unit="rate"
              height={170}
            />
          </div>
        ))}
      </div>
    );
  }

  if (shape === "logs") {
    const records = logRecordsOf(payload);
    if (records.length === 0) {
      return <EmptyState title="没有日志记录" />;
    }
    return (
      <div className="stack">
        {records.map((r) => (
          <div className="logrec" key={`${r.timestamp}-${r.message}`}>
            <div className="logrec__head">
              <span className="mono logrec__time">{formatInstantShort(r.timestamp)}</span>
              {r.level && (
                <Badge tone={r.level === "error" ? "critical" : "info"}>{r.level}</Badge>
              )}
              {r.traceId && <Ident className="mono faint">{r.traceId.slice(0, 16)}</Ident>}
            </div>
            <div className="logrec__msg">{r.message ?? "—"}</div>
            {Object.keys(r.labels).length > 0 && (
              <div className="logrec__labels">
                {Object.entries(r.labels).map(([k, v]) => (
                  <span key={k} className="logrec__label">
                    {k}={v}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    );
  }

  if (shape === "traces") {
    const spans = spanRecordsOf(payload);
    if (spans.length === 0) {
      return <EmptyState title="没有 span" />;
    }
    return (
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>开始</th>
              <th>服务</th>
              <th>操作</th>
              <th style={{ width: 96 }}>耗时</th>
              <th style={{ width: 96 }}>trace</th>
              <th style={{ width: 60 }}>错误</th>
            </tr>
          </thead>
          <tbody>
            {spans.map((s) => (
              <tr key={`${s.traceId}-${s.spanId}`}>
                <td className="mono faint">{formatInstantShort(s.startTime)}</td>
                <td className="cell-primary mono">{s.service ?? "—"}</td>
                <td className="mono">
                  <Ident>{s.operation ?? "—"}</Ident>
                </td>
                <td className="cell-num">
                  {s.durationUs === null ? "—" : `${(s.durationUs / 1000).toFixed(1)}ms`}
                </td>
                <td className="mono faint">
                  <Ident>{s.traceId ? `${s.traceId.slice(0, 12)}…` : "—"}</Ident>
                </td>
                <td>
                  {s.error === true ? (
                    <Badge tone="critical">是</Badge>
                  ) : s.error ? (
                    <Badge tone="critical">{s.error}</Badge>
                  ) : (
                    <span className="faint">否</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (shape === "events") {
    const events = eventRecordsOf(payload);
    if (events.length === 0) {
      return <EmptyState title="没有事件" />;
    }
    return (
      <div className="stack">
        {events.map((e) => (
          <div className="evt" key={`${e.changedAt}-${e.service}-${e.revision}`}>
            <span className="evt__when mono">{formatInstant(e.changedAt)}</span>
            <div style={{ minWidth: 0 }}>
              <div className="evt__summary">{e.summary ?? "—"}</div>
              <div className="evt__meta">
                {e.service && <Ident className="mono">{e.service}</Ident>}
                {e.changeKind && <span>{e.changeKind}</span>}
                {e.revision && <Ident className="mono">{e.revision}</Ident>}
              </div>
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="stack">
      <EmptyState
        title="未识别的数据结构"
        detail="这个证据的载荷不是已知的四种形态之一。下面仍然展示原始 JSON。"
      />
      <JsonViewer value={payload} maxHeight={260} />
    </div>
  );
}

/* ── relations ───────────────────────────────────────────────────────────── */

function RelationsView({
  runId,
  evidence,
  annotations,
}: {
  runId: string | null;
  evidence: EvidenceDetailVM[];
  annotations: Array<{
    id: string;
    sourceId: string;
    targetId: string;
    relationType: string;
    note: string | null;
    createdAt: string;
  }>;
}) {
  const [ruleRelations, setRuleRelations] = useState<
    Array<{
      id: string;
      hypothesisId: string;
      evidenceId: string;
      relationType: string;
      reason: string | null;
    }>
  >([]);
  const [relationError, setRelationError] = useState(false);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    setRelationError(false);
    void dataSource
      .getRelations(runId)
      .then((list) => {
        if (!alive) return;
        setRuleRelations(
          list.map((r) => ({
            id: r.id,
            hypothesisId: r.sourceId,
            evidenceId: r.targetId,
            relationType: r.relationType,
            reason: r.reason,
          })),
        );
      })
      .catch(() => alive && setRelationError(true));
    return () => {
      alive = false;
    };
  }, [runId]);

  const evidenceById = useMemo(() => new Map(evidence.map((e) => [e.id, e])), [evidence]);

  return (
    <div className="split">
      <Panel title="规则关系" meta={`${ruleRelations.length} 条`} className="panel--flush">
        {relationError ? (
          <ErrorState />
        ) : ruleRelations.length === 0 ? (
          <EmptyState title="没有关系边" detail="这份报告没有把候选和证据连起来。" />
        ) : (
          ruleRelations.map((r) => {
            const ev = evidenceById.get(r.evidenceId);
            return (
              <div className="rel" key={r.id}>
                <span className="rel__kind">{r.relationType}</span>
                <span className="rel__hyp mono">{r.hypothesisId.slice(0, 13)}…</span>
                <span className="rel__arrow">→</span>
                <span className="rel__ev mono">
                  {ev
                    ? `${ev.serviceId ?? "全局"} · ${ev.evidenceType}`
                    : r.evidenceId.slice(0, 13)}
                </span>
                {r.reason && <span className="rel__reason">{r.reason}</span>}
              </div>
            );
          })
        )}
      </Panel>

      <div className="stack">
        <Panel title="人工标注" meta={`${annotations.length} 条`}>
          {annotations.length === 0 ? (
            <EmptyState
              title="还没有人工标注"
              detail="标注保存在本机浏览器。后端开放写接口之前，它们不是权威记录。"
            />
          ) : (
            <div className="stack">
              {annotations.map((a) => (
                <div className="rel" key={a.id}>
                  <span className="rel__kind rel__kind--human">{a.relationType}</span>
                  <span className="rel__ev mono">{a.targetId.slice(0, 18)}…</span>
                  {a.note && <span className="rel__reason">{a.note}</span>}
                </div>
              ))}
            </div>
          )}
          <p className="section__note">
            人工标注当前只存在于浏览器（localStorage）。后端开放写入 API
            后，这里会改为读写真实存储。
          </p>
        </Panel>
      </div>
    </div>
  );
}

export const evidenceIcons = { External, X };
export const evidencePercent = formatPercent;
