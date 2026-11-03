/**
 * Ops — component health and the investigation funnel.
 *
 * Two questions live here: is the system healthy, and is work flowing through
 * it. The funnel is the second one: a run that was requested but never
 * produced a result is a silent failure, and it is the one worth watching.
 */
import { useEffect, useState } from "react";
import { dataSource } from "../data/source";
import { formatCount, formatInstant, formatPercent } from "../domain/format";
import type { HealthVM } from "../domain/types";
import { BarRow } from "../ui/charts";
import { Badge, EmptyState, ErrorState, Panel, Stat } from "../ui/primitives";

export function OpsPage() {
  const [health, setHealth] = useState<HealthVM | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    void dataSource
      .getHealth()
      .then((h) => alive && setHealth(h))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);

  if (failed) {
    return (
      <>
        <Head title="运维面板" sub="健康状态不可用。" />
        <Panel>
          <ErrorState />
        </Panel>
      </>
    );
  }

  if (!health) {
    return (
      <>
        <Head title="运维面板" sub="载入健康状态…" />
        <Panel>
          <EmptyState title="载入中" />
        </Panel>
      </>
    );
  }

  const funnel = health.funnel;
  const degraded = health.components.filter((c) => c.status === "DEGRADED");
  const down = health.components.filter((c) => c.status === "DOWN");
  const silent = health.components.filter((c) => c.claimedButSilent);

  return (
    <>
      <Head title="运维面板" sub={`最近检查 ${formatInstant(health.checkedAt)}`} />

      <div className="stat-row">
        <Stat
          label="组件"
          value={health.components.length}
          hint={`${health.components.filter((c) => c.status === "UP").length} 个正常`}
        />
        <Stat
          label="降级"
          value={degraded.length}
          tone={degraded.length > 0 ? "accent" : "neutral"}
          hint={degraded.map((c) => c.name).join(", ") || "无"}
        />
        <Stat
          label="不可用"
          value={down.length}
          tone={down.length > 0 ? "critical" : "neutral"}
          hint={down.map((c) => c.name).join(", ") || "无"}
        />
        <Stat
          label="领取但沉默"
          value={silent.length}
          tone={silent.length > 0 ? "critical" : "neutral"}
          hint="worker 领了活但不再上报心跳"
        />
      </div>

      <div className="split section">
        <div className="stack">
          <section className="section">
            <div className="section__head">
              <span className="label">组件</span>
              <div className="section__rule" />
              <span className="section__meta">{health.components.length} 个</span>
            </div>
            <Panel className="panel--flush">
              {health.components.length === 0 ? (
                <EmptyState title="没有组件数据" />
              ) : (
                health.components.map((c) => (
                  <div className="comp" key={c.name}>
                    <span className={`comp__dot comp__dot--${c.status}`} aria-hidden="true" />
                    <span className="comp__name">{c.name}</span>
                    <span className="comp__detail">{c.detail ?? "—"}</span>
                    {c.claimedButSilent && <Badge tone="critical">已领取 · 无心跳</Badge>}
                    <Badge
                      tone={
                        c.status === "UP"
                          ? "success"
                          : c.status === "DEGRADED"
                            ? "major"
                            : c.status === "DOWN"
                              ? "critical"
                              : "neutral"
                      }
                    >
                      {c.status}
                    </Badge>
                  </div>
                ))
              )}
            </Panel>
          </section>

          {funnel && (
            <section className="section">
              <div className="section__head">
                <span className="label">告警折叠</span>
                <div className="section__rule" />
                <span className="section__meta">自 {formatInstant(funnel.since)}</span>
              </div>
              <Panel>
                <div className="stack">
                  <BarRow
                    label="重复告警被折叠"
                    ratio={
                      funnel.alertsSeen > 0 ? funnel.foldedDuplicates / funnel.alertsSeen : null
                    }
                    display={`${formatCount(funnel.foldedDuplicates)} / ${formatCount(funnel.alertsSeen)}`}
                    note="同一故障的重复告警被合并成一起事故"
                    tone="info"
                  />
                  <BarRow
                    label="告警转事故"
                    ratio={
                      funnel.alertsSeen > 0 ? funnel.alertIncidents / funnel.alertsSeen : null
                    }
                    display={`${formatCount(funnel.alertIncidents)} / ${formatCount(funnel.alertsSeen)}`}
                    note="只有告警升级来的事故算在这里"
                    tone="accent"
                  />
                  <BarRow
                    label="人工建的事故"
                    ratio={
                      funnel.alertsSeen > 0 ? funnel.manualIncidents / funnel.alertsSeen : null
                    }
                    display={formatCount(funnel.manualIncidents)}
                    note="人直接建的，没有经过告警"
                    tone="neutral"
                  />
                </div>
                <p className="section__note">
                  折叠率越高，说明同一故障触发的告警越多——这是告警质量问题，不是调查质量问题。
                  告警转事故只数告警升级来的那部分；人工建的事故单独列，不混进比例里。
                </p>
              </Panel>
            </section>
          )}
        </div>

        <div className="stack">
          {funnel && (
            <section className="section">
              <div className="section__head">
                <span className="label">调查漏斗</span>
                <div className="section__rule" />
              </div>
              <Panel>
                <div className="stack">
                  <BarRow
                    label="已发起"
                    ratio={funnel.runsRequested > 0 ? 1 : null}
                    display={formatCount(funnel.runsRequested)}
                    note="请求发起的调查数"
                    tone="neutral"
                  />
                  <BarRow
                    label="已完成"
                    ratio={
                      funnel.runsRequested > 0
                        ? funnel.runsCompleted / funnel.runsRequested
                        : null
                    }
                    display={`${funnel.runsCompleted} / ${funnel.runsRequested}`}
                    tone="supported"
                  />
                  <BarRow
                    label="失败"
                    ratio={
                      funnel.runsRequested > 0 ? funnel.runsFailed / funnel.runsRequested : null
                    }
                    display={`${funnel.runsFailed} / ${funnel.runsRequested}`}
                    tone="critical"
                  />
                  <BarRow
                    label="无结论"
                    ratio={
                      funnel.runsRequested > 0
                        ? funnel.runsWithoutResult / funnel.runsRequested
                        : null
                    }
                    display={`${funnel.runsWithoutResult} / ${funnel.runsRequested}`}
                    note="跑完了但没给出结论"
                    tone="accent"
                  />
                </div>
                <p className="section__note">
                  「无结论」比「失败」更值得盯：它不会报错，只是安静地什么都没说。
                </p>
              </Panel>
            </section>
          )}

          {funnel && (
            <section className="section">
              <div className="section__head">
                <span className="label">人工介入</span>
                <div className="section__rule" />
              </div>
              <Panel>
                <Stat
                  label="待人工复核"
                  value={formatCount(funnel.needsHumanReview)}
                  hint="调查跑完了，但结论要人拍板"
                  tone={funnel.needsHumanReview > 0 ? "accent" : "neutral"}
                />
                <p className="section__note">
                  待复核 {funnel.needsHumanReview} 起 · 复核率{" "}
                  {formatPercent(
                    funnel.runsCompleted > 0
                      ? funnel.needsHumanReview / funnel.runsCompleted
                      : null,
                  )}
                </p>
              </Panel>
            </section>
          )}

          <section className="section">
            <div className="section__head">
              <span className="label">尚未接入</span>
              <div className="section__rule" />
            </div>
            <Panel>
              <div className="notes">
                {[
                  "报告正文读取接口（rca_report_id → 报告）",
                  "证据查询与关系写入接口",
                  "运行进度 SSE（当前只有快照）",
                  "outbox / redis / publish-probe 诊断端点",
                  "会话日志读取",
                ].map((t) => (
                  <div className="note" key={t}>
                    <span className="note__bullet" />
                    <span>
                      {t} <span className="faint">— 待后端提供</span>
                    </span>
                  </div>
                ))}
              </div>
              <p className="section__note">
                这里列的是控制面还没暴露的读面。等接口到位后，页面从 fixture 切到真实数据，
                布局不变。
              </p>
            </Panel>
          </section>
        </div>
      </div>
    </>
  );
}

function Head({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="page-head">
      <div className="page-head__text">
        <div className="eyebrow">
          <span className="label">运维</span>
        </div>
        <h1 className="page-title">{title}</h1>
        <p className="page-sub">{sub}</p>
      </div>
    </div>
  );
}
