/**
 * Incident Workbench — the page that matters.
 *
 * Reading order matches how an operator actually works:
 *   conclusion → why we believe it → the raw evidence behind each claim →
 *   what we could not check → what the investigation actually did → cost.
 *
 * Two distinctions are load-bearing and must not be flattened:
 *   missing_data — we could not look (a gap in the tooling)
 *   no_data      — we looked and found nothing (this can support a REFUTED)
 */
import { useEffect, useState } from "react";
import { ArrowRight, Check, External, X } from "../app/icons";
import { dataSource, type EvidenceDetailVM } from "../data/source";
import {
  evidenceTypeLabel,
  faultLabel,
  formatCount,
  formatInstant,
  formatInstantShort,
  formatTokens,
  NODE_LABELS,
  STAGE_LABELS,
  VERDICT_LABELS,
  WAITING_ON_LABELS,
} from "../domain/format";
import type { IncidentVM, ReportVM, RunVM } from "../domain/types";
import {
  Badge,
  Button,
  Copyable,
  EmptyState,
  ErrorState,
  Hash,
  JsonViewer,
  KeyValue,
  Meter,
  Panel,
  SeverityBadge,
  StageBadge,
  VerdictBadge,
} from "../ui/primitives";

export function IncidentPage({
  incidentId,
  runId,
  focusRun,
}: {
  incidentId: string;
  runId?: string;
  focusRun?: boolean;
}) {
  const [incident, setIncident] = useState<IncidentVM | null>(null);
  const [missing, setMissing] = useState(false);
  const [report, setReport] = useState<ReportVM | null>(null);
  const [run, setRun] = useState<RunVM | null>(null);
  const [evidence, setEvidence] = useState<EvidenceDetailVM[]>([]);
  const [selected, setSelected] = useState<EvidenceDetailVM | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoaded(false);
    setMissing(false);
    void dataSource
      .getIncident(incidentId)
      .then((inc) => {
        if (!alive) return;
        if (!inc) {
          setMissing(true);
          return;
        }
        setIncident(inc);
      })
      .catch(() => alive && setMissing(true));
    return () => {
      alive = false;
    };
  }, [incidentId]);

  const effectiveRunId = runId ?? incident?.currentRunId ?? null;

  useEffect(() => {
    if (!effectiveRunId) {
      setReport(null);
      setRun(null);
      setEvidence([]);
      setLoaded(true);
      return;
    }
    let alive = true;
    void Promise.all([
      dataSource.getReport(effectiveRunId),
      dataSource.getRun(effectiveRunId),
      dataSource.getEvidence(effectiveRunId),
    ]).then(([r, u, e]) => {
      if (!alive) return;
      setReport(r);
      setRun(u);
      setEvidence(e);
      setLoaded(true);
    });
    return () => {
      alive = false;
    };
  }, [effectiveRunId]);

  if (missing) {
    return (
      <>
        <div className="page-head">
          <div className="page-head__text">
            <span className="label">事故</span>
            <h1 className="page-title">找不到这笔事故</h1>
            <p className="page-sub">
              标识 <span className="mono">{incidentId}</span> 在当前数据源中不存在。
            </p>
          </div>
        </div>
        <Panel>
          <ErrorState
            detail={
              <>
                控制面尚未接入时，这里显示的是 fixture
                数据。真实接入后，失效的链接会落到这个页面。
              </>
            }
          />
        </Panel>
      </>
    );
  }

  if (!incident) {
    return (
      <div className="stack">
        <div className="page-head">
          <div className="page-head__text">
            <span className="label">事故</span>
            <h1 className="page-title">载入中…</h1>
          </div>
        </div>
        <Panel>
          <div className="stack">
            <Meter value={0.4} />
            <div className="faint">正在读取事故详情</div>
          </div>
        </Panel>
      </div>
    );
  }

  const stage = incident.stage;

  return (
    <>
      <div className="wb-head">
        <div className="wb-head__main">
          <div className="row" style={{ marginBottom: "var(--space-3)" }}>
            <SeverityBadge severity={incident.severity} />
            {stage && (
              <StageBadge
                kind={stage.kind}
                label={STAGE_LABELS[stage.kind] ?? "未知"}
                running={stage.kind === "RUNNING"}
              />
            )}
            <Badge>{incident.origin}</Badge>
            <span className="mono faint" style={{ fontSize: "var(--text-xs)" }}>
              {incident.id}
            </span>
          </div>
          <h1 className="wb-title">{incident.title}</h1>
          <p className="wb-sub">
            <span className="mono">{incident.service}</span>
            {stage?.detail ? ` · ${stage.detail}` : ""}
          </p>
        </div>
        <div className="wb-actions">
          <Button size="sm">指派</Button>
          <Button size="sm">确认</Button>
          <Button size="sm" variant="primary">
            流转
          </Button>
        </div>
      </div>

      <div className="stat-row" style={{ marginBottom: "var(--space-5)" }}>
        <Stat2
          label="状态"
          value={stage ? STAGE_LABELS[stage.kind] : "—"}
          hint={stage?.waitingOn ? `等${WAITING_ON_LABELS[stage.waitingOn]}` : undefined}
        />
        <Stat2 label="负责" value={incident.assignee ?? "未指派"} />
        <Stat2
          label="创建"
          value={formatInstantShort(incident.createdAt)}
          hint={formatInstant(incident.createdAt)}
        />
        <Stat2
          label="重复告警"
          value={incident.duplicateAlerts ? `×${incident.duplicateAlerts}` : "无"}
          hint={incident.origin === "ALERT" ? "来自告警" : "人工创建"}
        />
        <Stat2
          label="重试"
          value={incident.retryAttempts ?? 0}
          hint={
            incident.nextRetryAt
              ? `下次 ${formatInstantShort(incident.nextRetryAt)}`
              : undefined
          }
        />
      </div>

      <div className="split">
        <div className="stack">
          {/* ── conclusion ───────────────────────────────────────────── */}
          <section className="section">
            <div className="section__head">
              <span className="label">结论</span>
              <div className="section__rule" />
              {report && <span className="section__meta">{report.runId}</span>}
            </div>
            {report ? (
              <VerdictBlock report={report} />
            ) : (
              <Panel>
                <EmptyState
                  title="还没有结论"
                  detail={
                    effectiveRunId
                      ? "调查仍在进行，或这次运行没有产出报告。"
                      : "这笔事故还没有关联的调查运行。"
                  }
                />
              </Panel>
            )}
          </section>

          {/* ── hypotheses ───────────────────────────────────────────── */}
          {report && report.hypotheses.length > 0 && (
            <section className="section">
              <div className="section__head">
                <span className="label">候选与证据</span>
                <div className="section__rule" />
                <span className="section__meta">{report.hypotheses.length} 个候选</span>
              </div>
              <Panel>
                {report.hypotheses.map((h) => (
                  <div className="hyp" key={h.id}>
                    <span className={`hyp__rank${h.rank === 1 ? " hyp__rank--top" : ""}`}>
                      {h.rank ?? "—"}
                    </span>
                    <div style={{ minWidth: 0 }}>
                      <div className="hyp__name">
                        {h.service ?? "?"} / {h.component ?? "?"} /{" "}
                        <span style={{ color: "var(--text-secondary)" }}>
                          {faultLabel(h.faultType)}
                        </span>
                      </div>
                      {h.mechanism && <div className="hyp__mech">{h.mechanism}</div>}
                      {h.predictedObservations.length > 0 && (
                        <div className="hyp__pred">
                          {h.predictedObservations.map((p) => (
                            <Badge key={p}>{p}</Badge>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="hyp__counts">
                      <span className="hyp__count hyp__count--s">
                        <Check size={11} />
                        {h.supportCount}
                      </span>
                      <span className="hyp__count hyp__count--r">
                        <X size={11} />
                        {h.refuteCount}
                      </span>
                    </div>
                  </div>
                ))}
              </Panel>
            </section>
          )}

          {/* ── evidence ─────────────────────────────────────────────── */}
          <section className="section">
            <div className="section__head">
              <span className="label">证据</span>
              <div className="section__rule" />
              <span className="section__meta">{evidence.length} 条</span>
            </div>
            <Panel className="panel--flush">
              {evidence.length === 0 ? (
                <EmptyState
                  title={loaded ? "没有证据" : "正在读取证据"}
                  detail={
                    loaded
                      ? "这次运行没有产出可引用的证据条目。"
                      : "调查的证据正在从证据库拉取。"
                  }
                />
              ) : (
                evidence.map((e) => (
                  <button
                    key={e.id}
                    type="button"
                    className="ev"
                    onClick={() => setSelected(e)}
                  >
                    <span className="ev__icon">
                      {evidenceTypeLabel(e.evidenceType).slice(0, 2)}
                    </span>
                    <span style={{ minWidth: 0 }}>
                      <span className="ev__title">
                        {e.serviceId ?? "—"} · {evidenceTypeLabel(e.evidenceType)}
                      </span>
                      <span className="ev__meta">
                        <span>{formatInstantShort(e.observedAt)}</span>
                        {e.queryTemplateId && <span>{e.queryTemplateId}</span>}
                        <Hash value={e.checksum} />
                      </span>
                    </span>
                    <span className="ev__right">
                      <span>{e.sourceLink ? "有源链接" : "—"}</span>
                      <ArrowRight size={12} />
                    </span>
                  </button>
                ))
              )}
            </Panel>
          </section>

          {/* ── what we could not check ─────────────────────────────── */}
          {report && (report.missingData.length > 0 || report.noData.length > 0) && (
            <section className="section">
              <div className="section__head">
                <span className="label">不确定的地方</span>
                <div className="section__rule" />
              </div>
              <div className="stack">
                {report.missingData.length > 0 && (
                  <div className="callout callout--warning">
                    <span className="callout__mark">missing</span>
                    <div>
                      <strong style={{ color: "var(--text-primary)", fontWeight: 560 }}>
                        没法查（{report.missingData.length}）
                      </strong>
                      <div className="notes" style={{ marginTop: "var(--space-2)" }}>
                        {report.missingData.map((m) => (
                          <div className="note note--missing" key={m}>
                            <span className="note__bullet" />
                            <span>{m}</span>
                          </div>
                        ))}
                      </div>
                      <p className="section__note">
                        这些是工具本身够不到的地方。它们不是「查了没有」，而是「没查成」——
                        不构成对任何候选的反驳。
                      </p>
                    </div>
                  </div>
                )}
                {report.noData.length > 0 && (
                  <div className="callout callout--info">
                    <span className="callout__mark">no data</span>
                    <div>
                      <strong style={{ color: "var(--text-primary)", fontWeight: 560 }}>
                        查了，但没有（{report.noData.length}）
                      </strong>
                      <div className="notes" style={{ marginTop: "var(--space-2)" }}>
                        {report.noData.map((m) => (
                          <div className="note note--nodata" key={m}>
                            <span className="note__bullet" />
                            <span>{m}</span>
                          </div>
                        ))}
                      </div>
                      <p className="section__note">
                        查询成功但结果为空。这类「没有」是可以用来反驳候选的——
                        和上面那种「没法查」性质不同。
                      </p>
                    </div>
                  </div>
                )}
              </div>
            </section>
          )}
        </div>

        {/* ── right column ─────────────────────────────────────────── */}
        <div className="stack">
          {/* run timeline */}
          <section className="section">
            <div className="section__head">
              <span className="label">调查过程</span>
              <div className="section__rule" />
            </div>
            <Panel>
              {run && run.steps.length > 0 ? (
                <>
                  <div className="timeline">
                    {run.steps.map((s, i) => {
                      const isLast = i === run.steps.length - 1;
                      return (
                        <div className="tl" key={`${s.stepIndex}-${s.node}`}>
                          <span
                            className={`tl__dot${isLast ? " tl__dot--active" : " tl__dot--done"}`}
                          />
                          <div className="tl__head">
                            <span className="tl__node">{NODE_LABELS[s.node] ?? s.node}</span>
                            <span className="tl__desc">{s.description}</span>
                          </div>
                          <div className="tl__meta">
                            <span>step {s.stepIndex}</span>
                            <span>证据 {s.evidenceCount}</span>
                            {s.at && <span>{formatInstantShort(s.at)}</span>}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                  {focusRun && (
                    <div className="section__note">
                      这是运行 <span className="mono">{run.runId}</span> 的完整步骤。
                    </div>
                  )}
                </>
              ) : (
                <EmptyState
                  title="还没有运行记录"
                  detail="调查开始后，每一步会实时出现在这里。"
                />
              )}
            </Panel>
          </section>

          {/* incident facts */}
          <section className="section">
            <div className="section__head">
              <span className="label">事故属性</span>
              <div className="section__rule" />
            </div>
            <Panel>
              <KeyValue
                items={[
                  { label: "服务", value: <span className="mono">{incident.service}</span> },
                  { label: "状态", value: incident.status },
                  { label: "来源", value: incident.origin },
                  {
                    label: "观测窗口",
                    value: (
                      <span className="mono">
                        {formatInstantShort(incident.windowStart)} →{" "}
                        {formatInstantShort(incident.windowEnd)}
                      </span>
                    ),
                  },
                  {
                    label: "ACK",
                    value: incident.acknowledgedAt
                      ? formatInstant(incident.acknowledgedAt)
                      : "未确认",
                  },
                  {
                    label: "结论确认",
                    value: incident.confirmation
                      ? `${incident.confirmation === "ACCEPTED" ? "已接受" : "已驳回"} · ${formatInstantShort(incident.confirmedAt)}`
                      : "待确认",
                  },
                  {
                    label: "运行",
                    value: incident.currentRunId ? (
                      <a
                        href={`/incidents/${incident.id}/runs/${incident.currentRunId}`}
                        className="mono"
                        style={{ color: "var(--accent-strong)" }}
                      >
                        {incident.currentRunId}
                      </a>
                    ) : (
                      "—"
                    ),
                  },
                ]}
              />
            </Panel>
          </section>

          {/* cost & digests */}
          {report && (
            <section className="section">
              <div className="section__head">
                <span className="label">成本与配置</span>
                <div className="section__rule" />
              </div>
              <Panel>
                <KeyValue
                  items={[
                    { label: "工具调用", value: formatCount(report.cost.toolCalls) },
                    { label: "tokens", value: formatTokens(report.cost.tokens) },
                    {
                      label: "受影响服务",
                      value: report.affectedServices.join(", ") || "—",
                    },
                    { label: "最早异常", value: formatInstant(report.rootCauseTime) },
                    { label: "profile", value: <Hash value={report.profileDigest} /> },
                    { label: "strategy", value: <Hash value={report.strategyDigest} /> },
                  ]}
                />
                <p className="section__note">
                  「最早异常」是<b>观测到</b>
                  的时间，不是告警声称的时间。两者不一致本身就是线索。
                </p>
              </Panel>
            </section>
          )}

          {/* rejected proposals */}
          {report && report.rejectedProposals.length > 0 && (
            <section className="section">
              <div className="section__head">
                <span className="label">被丢弃的提案</span>
                <div className="section__rule" />
              </div>
              <Panel>
                {report.rejectedProposals.map((p) => (
                  <div className="note" key={`${p.reasonCode}-${p.proposal}`}>
                    <span className="note__bullet" />
                    <span>
                      <span className="mono" style={{ color: "var(--text-primary)" }}>
                        {p.proposal}
                      </span>
                      <div className="faint" style={{ fontSize: "var(--text-xs)" }}>
                        {p.reasonCode}
                        {p.detail ? ` · ${p.detail}` : ""}
                      </div>
                    </span>
                  </div>
                ))}
              </Panel>
            </section>
          )}
        </div>
      </div>

      {selected && <EvidenceDrawer evidence={selected} onClose={() => setSelected(null)} />}
    </>
  );
}

function Stat2({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
}) {
  return (
    <div className="stat">
      <div className="stat__label">{label}</div>
      <div className="stat__value" style={{ fontSize: "var(--text-lg)" }}>
        {value}
      </div>
      {hint && <div className="stat__hint">{hint}</div>}
    </div>
  );
}

function VerdictBlock({ report }: { report: ReportVM }) {
  const led = report.abstained
    ? "var(--verdict-abstained)"
    : report.verdict === "SUPPORTED"
      ? "var(--verdict-supported)"
      : report.verdict === "REFUTED"
        ? "var(--verdict-refuted)"
        : "var(--verdict-inconclusive)";

  return (
    <div className="verdict">
      <div className="verdict__bar">
        <span className="verdict__led" style={{ background: led }} />
        <span className="verdict__label">判断</span>
        <VerdictBadge
          verdict={report.verdict}
          abstained={report.abstained}
          label={VERDICT_LABELS[report.verdict] ?? "结论缺失"}
        />
        {report.abstained && (
          <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
            证据不足时的正确结局——比猜一个更像
          </span>
        )}
        <div className="spacer" />
        {report.generatedAt && (
          <span className="section__meta">{formatInstant(report.generatedAt)}</span>
        )}
      </div>
      <div className="verdict__body">
        <p className="verdict__summary">{report.summary}</p>
        <div className="verdict__meta">
          <div className="verdict__meta-item">
            <span className="verdict__meta-k">根因服务</span>
            <span className="verdict__meta-v mono">{report.hypotheses[0]?.service ?? "—"}</span>
          </div>
          <div className="verdict__meta-item">
            <span className="verdict__meta-k">故障类型</span>
            <span className="verdict__meta-v">
              {faultLabel(report.hypotheses[0]?.faultType)}
            </span>
          </div>
          <div className="verdict__meta-item">
            <span className="verdict__meta-k">最早异常（观测）</span>
            <span className="verdict__meta-v mono">{formatInstant(report.rootCauseTime)}</span>
          </div>
          <div className="verdict__meta-item">
            <span className="verdict__meta-k">受影响</span>
            <span className="verdict__meta-v mono">
              {report.affectedServices.join(", ") || "—"}
            </span>
          </div>
        </div>

        {report.propagationChain.length > 0 && (
          <div className="chain" style={{ marginTop: "var(--space-5)" }}>
            {report.propagationChain.map(([from, to], i) => (
              <span key={`${from}-${to}`} style={{ display: "contents" }}>
                {i > 0 && (
                  <span className="chain__arrow">
                    <ArrowRight size={14} />
                  </span>
                )}
                <span className={`chain__node${i === 0 ? " chain__node--origin" : ""}`}>
                  <span className="chain__svc">{to}</span>
                  <span className="chain__role">{i === 0 ? "起点" : "下游"}</span>
                </span>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function EvidenceDrawer({
  evidence,
  onClose,
}: {
  evidence: EvidenceDetailVM;
  onClose: () => void;
}) {
  return (
    <>
      {/* A real button: the backdrop is clickable, so it needs to be focusable
          and announce itself. Clicking it closes the drawer. */}
      <button
        type="button"
        className="drawer-scrim"
        aria-label="关闭证据详情"
        onClick={onClose}
      />
      <aside className="drawer" role="dialog" aria-label="证据详情">
        <header className="drawer__head">
          <span className="drawer__title">
            {evidenceTypeLabel(evidence.evidenceType)} · {evidence.serviceId ?? "—"}
          </span>
          <div className="spacer" />
          {evidence.sourceLink && (
            <a
              className="ghost-btn"
              href={evidence.sourceLink}
              target="_blank"
              rel="noreferrer"
              title="打开源链接"
            >
              <External size={14} />
            </a>
          )}
          <button type="button" className="ghost-btn" onClick={onClose} aria-label="关闭">
            <X size={14} />
          </button>
        </header>
        <div className="drawer__body stack">
          <Panel>
            <KeyValue
              items={[
                {
                  label: "证据 ID",
                  value: <Copyable value={evidence.id} display={evidence.id} />,
                },
                { label: "类型", value: evidenceTypeLabel(evidence.evidenceType) },
                {
                  label: "服务",
                  value: <span className="mono">{evidence.serviceId ?? "—"}</span>,
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
                  value: <span className="mono">{evidence.queryTemplateId ?? "—"}</span>,
                },
                { label: "checksum", value: <Hash value={evidence.checksum} /> },
              ]}
            />
          </Panel>

          {evidence.renderedQuery && (
            <Panel title="实际查询">
              <pre className="json">{evidence.renderedQuery}</pre>
            </Panel>
          )}

          <Panel title="归一化数据">
            <JsonViewer value={evidence.normalizedData} maxHeight={280} />
          </Panel>

          <Panel title="原始负载">
            <JsonViewer value={evidence.rawPayload} maxHeight={320} />
          </Panel>
        </div>
      </aside>
    </>
  );
}
