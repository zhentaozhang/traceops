/**
 * Overview — the triage board.
 *
 * Ordered by *lane* (who is the incident waiting on), not by severity: the
 * operator's first question is "what needs a human", not "what is worst".
 */
import { useEffect, useMemo, useState } from "react";
import { ArrowRight, Clock, User, Worker } from "../app/icons";
import { navigate } from "../app/router";
import { dataSource } from "../data/source";
import { urgencyOf } from "../domain/adapters";
import {
  formatCount,
  formatInstant,
  formatRelative,
  LANE_LABELS,
  SEVERITY_LABELS,
  STAGE_LABELS,
  WAITING_ON_LABELS,
} from "../domain/format";
import type { HealthVM, IncidentVM } from "../domain/types";
import { EmptyState, Panel, SeverityBadge, StageBadge, Stat } from "../ui/primitives";

const LANE_ORDER = ["waiting-person", "failed", "active", "waiting-clock", "finished"];

export function OverviewPage({ incidents }: { incidents: IncidentVM[] }) {
  const [health, setHealth] = useState<HealthVM | null>(null);

  useEffect(() => {
    void dataSource.getHealth().then(setHealth);
  }, []);

  const sorted = useMemo(
    () =>
      [...incidents].sort(
        (a, b) =>
          urgencyOf(a.stage) - urgencyOf(b.stage) || a.createdAt.localeCompare(b.createdAt),
      ),
    [incidents],
  );

  const lanes = useMemo(() => {
    const buckets = new Map<string, IncidentVM[]>();
    for (const inc of sorted) {
      const lane = inc.stage?.lane ?? "waiting-clock";
      const list = buckets.get(lane) ?? [];
      list.push(inc);
      buckets.set(lane, list);
    }
    return LANE_ORDER.map((lane) => ({ lane, items: buckets.get(lane) ?? [] })).filter(
      (g) => g.items.length > 0,
    );
  }, [sorted]);

  const funnel = health?.funnel ?? null;

  return (
    <>
      <div className="page-head">
        <div className="page-head__text">
          <div className="eyebrow">
            <span className="label">值班概览</span>
            {health?.checkedAt && (
              <span className="faint" style={{ fontSize: "var(--text-2xs)" }}>
                {formatInstant(health.checkedAt)}
              </span>
            )}
          </div>
          <h1 className="page-title">今天需要人看的事</h1>
          <p className="page-sub">
            事故按「在等谁」分组，而不是按严重度。等人决策的排在最前——它们是唯一一类
            机器推不动、只能人接手的事。
          </p>
        </div>
      </div>

      {funnel && (
        <div className="stat-row">
          <Stat
            label="告警折叠"
            value={formatCount(funnel.foldedDuplicates)}
            hint={`${funnel.alertsSeen} 条告警 → ${funnel.alertIncidents} 起告警事故 + ${funnel.manualIncidents} 起人工`}
          />
          <Stat
            label="调查已发起"
            value={`${funnel.runsCompleted}/${funnel.runsRequested}`}
            hint={`${funnel.runsFailed} 次失败 · ${funnel.runsWithoutResult} 次无结论`}
            tone={funnel.runsFailed > 0 ? "critical" : "neutral"}
          />
          <Stat
            label="待人工复核"
            value={formatCount(funnel.needsHumanReview)}
            hint="调查跑完了，但结论要人拍板"
            tone={funnel.needsHumanReview > 0 ? "accent" : "neutral"}
          />
          <Stat
            label="重复率"
            value={
              funnel.alertsSeen > 0
                ? `${Math.round((funnel.foldedDuplicates / funnel.alertsSeen) * 100)}%`
                : "—"
            }
            hint="被折叠成同一事故的告警占比"
          />
        </div>
      )}

      <div className="section">
        <div className="section__head">
          <span className="label">分诊</span>
          <div className="section__rule" />
          <span className="section__meta">{incidents.length} 起事故</span>
        </div>

        {lanes.length === 0 ? (
          <Panel>
            <EmptyState
              title="当前没有事故"
              detail="当控制面接入后，这里会按等待对象自动分诊。"
            />
          </Panel>
        ) : (
          <div className="stack">
            {lanes.map(({ lane, items }) => (
              <section key={lane} className="lane">
                <header className="lane__head">
                  <span className={`lane__marker lane__marker--${lane}`} aria-hidden="true" />
                  <h2 className="lane__title">{LANE_LABELS[lane] ?? lane}</h2>
                  <span className="lane__count num">{items.length}</span>
                  <div className="section__rule" />
                </header>
                <Panel className="panel--flush">
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th style={{ width: 92 }}>严重度</th>
                          <th>服务</th>
                          <th>标题</th>
                          <th style={{ width: 128 }}>状态</th>
                          <th style={{ width: 96 }}>负责</th>
                          <th style={{ width: 108 }}>创建</th>
                          <th style={{ width: 32 }} />
                        </tr>
                      </thead>
                      <tbody>
                        {items.map((inc) => (
                          <tr
                            key={inc.id}
                            className="is-clickable"
                            onClick={() => navigate(`/incidents/${inc.id}`)}
                          >
                            <td>
                              <SeverityBadge severity={inc.severity} />
                            </td>
                            <td className="cell-primary mono">{inc.service}</td>
                            <td className="clip" style={{ maxWidth: 420 }}>
                              {inc.title}
                            </td>
                            <td>
                              <StageBadge
                                kind={inc.stage?.kind ?? "UNKNOWN"}
                                label={STAGE_LABELS[inc.stage?.kind ?? "UNKNOWN"] ?? "未知"}
                                running={inc.stage?.kind === "RUNNING"}
                              />
                            </td>
                            <td>
                              {inc.assignee ? (
                                <span className="mono dim">{inc.assignee}</span>
                              ) : (
                                <span className="faint">—</span>
                              )}
                            </td>
                            <td className="faint" title={formatInstant(inc.createdAt)}>
                              {formatRelative(inc.createdAt)}
                            </td>
                            <td className="faint">
                              <ArrowRight size={13} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Panel>
              </section>
            ))}
          </div>
        )}
      </div>
    </>
  );
}

/** Small legend explaining what each lane means. */
export function LaneLegend() {
  const entries: Array<
    [string, string, "active" | "waiting-person" | "waiting-clock" | "finished" | "failed"]
  > = [
    ["active", "机器在跑，等它", "active"],
    ["waiting-person", "等人决策", "waiting-person"],
    ["waiting-clock", "等重试窗口", "waiting-clock"],
    ["finished", "已收口", "finished"],
    ["failed", "失败待人工介入", "failed"],
  ];
  return (
    <div className="row" style={{ gap: "var(--space-5)" }}>
      {entries.map(([key, label]) => (
        <span key={key} className="legend-item">
          <span className={`lane__marker lane__marker--${key}`} aria-hidden="true" />
          <span className="dim">{label}</span>
        </span>
      ))}
    </div>
  );
}

export const laneIcons = { Clock, User, Worker };
export const severityLabels = SEVERITY_LABELS;
export const waitingLabels = WAITING_ON_LABELS;
