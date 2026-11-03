/**
 * Evaluation Lab.
 *
 * The evaluation harness already produces the numbers; this page refuses to
 * make them look better than they are. The synthetic-data note stays on
 * screen, the naive baselines stay beside our numbers, and the chance
 * baseline is always shown as the floor to beat.
 */
import { useEffect, useMemo, useState } from "react";
import { dataSource, type EvaluationVM } from "../data/source";
import { formatCount, formatInstant, formatPercent } from "../domain/format";
import { BarRow, CompareBars } from "../ui/charts";
import { Badge, EmptyState, ErrorState, Hash, Panel, Stat } from "../ui/primitives";

export function EvaluationPage() {
  const [data, setData] = useState<EvaluationVM | null>(null);
  const [failed, setFailed] = useState(false);
  const [arm, setArm] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void dataSource
      .getEvaluation()
      .then((d) => alive && setData(d))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);

  const agg = data?.aggregate ?? {};
  const chance = data?.chanceBaseline ?? {};
  const naive = data?.naiveBaselines ?? {};

  const peak = asRecord(naive["peak_metric"]);
  const earliest = asRecord(naive["earliest_anomaly"]);

  const scenarios = useMemo(() => data?.scenarios ?? [], [data]);

  const armIds = useMemo(() => Object.keys(data?.arms ?? {}).sort(), [data]);
  const selectedArm = arm ?? armIds[armIds.length - 1] ?? null;

  const armResults = selectedArm ? (data?.armResults?.[selectedArm] ?? []) : [];

  if (failed) {
    return (
      <>
        <PageHead title="评测实验室" sub="读取评测报告失败。" />
        <Panel>
          <ErrorState />
        </Panel>
      </>
    );
  }

  if (!data) {
    return (
      <>
        <PageHead title="评测实验室" sub="载入评测报告…" />
        <Panel>
          <EmptyState title="载入中" />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHead
        title="评测实验室"
        sub={`模式 ${data.mode ?? "未知"} · ${formatCount(agg["scenario_count"] ?? null)} 个场景 · 生成于 ${formatInstant(data.generatedAt)}`}
        note={data.note}
      />

      {/* headline numbers */}
      <div className="stat-row">
        <Stat
          label="Top-1 命中"
          value={formatPercent(agg["ac_at_1"] ?? null)}
          hint={`随机基线 ${formatPercent(chance["chance_at_1"] ?? null)} · 提升 ${(chance["lift_at_1"] ?? 0).toFixed(2)}×`}
          tone="success"
        />
        <Stat
          label="结论正确"
          value={formatPercent(agg["diagnosis_accuracy"] ?? null)}
          hint="含弃权场景的判定"
        />
        <Stat
          label="错误断言"
          value={formatPercent(agg["wrong_answer_rate"] ?? null)}
          hint="在没有证据时下断言的比例"
          tone={(agg["wrong_answer_rate"] ?? 0) > 0 ? "critical" : "success"}
        />
        <Stat
          label="弃权率"
          value={formatPercent(agg["abstention_rate"] ?? null)}
          hint="证据不足时选择不回答"
        />
        <Stat
          label="平均工具调用"
          value={(agg["mean_tool_calls"] ?? 0).toFixed(1)}
          hint={`MRR ${(agg["mrr"] ?? 0).toFixed(3)}`}
        />
      </div>

      <div className="split section">
        <div className="stack">
          {/* candidate ranking quality */}
          <section className="section">
            <div className="section__head">
              <span className="label">候选排序质量</span>
              <div className="section__rule" />
              <span className="section__meta">对照随机猜测</span>
            </div>
            <Panel>
              <div className="stack">
                <BarRow
                  label="Top-1"
                  ratio={agg["ac_at_1"] ?? null}
                  note={`Top-3 ${formatPercent(agg["ac_at_3"] ?? null)} · Top-5 ${formatPercent(agg["ac_at_5"] ?? null)}`}
                  tone="accent"
                />
                <BarRow
                  label="Top-3"
                  ratio={agg["ac_at_3"] ?? null}
                  note={`随机 ${formatPercent(chance["chance_at_3"] ?? null)}`}
                  tone="info"
                />
                <BarRow
                  label="Top-5"
                  ratio={agg["ac_at_5"] ?? null}
                  note={`随机 ${formatPercent(chance["chance_at_5"] ?? null)}`}
                  tone="info"
                />
                <BarRow
                  label="平均倒数排名 MRR"
                  ratio={agg["mrr"] ?? null}
                  note="越高越好"
                  tone="supported"
                />
              </div>
            </Panel>
          </section>

          {/* naive baselines — the honest comparison */}
          <section className="section">
            <div className="section__head">
              <span className="label">和朴素基线比</span>
              <div className="section__rule" />
            </div>
            <Panel>
              <div className="stack">
                <div>
                  <div className="row" style={{ marginBottom: "var(--space-3)" }}>
                    <span className="mono" style={{ color: "var(--text-primary)" }}>
                      peak metric
                    </span>
                    <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
                      最高指标就是根因？基线弃权 {asText(peak["abstained"])} 起
                    </span>
                  </div>
                  <CompareBars
                    ours={{ label: "本系统", ratio: asNumber(peak["ours_at_1"]) }}
                    theirs={{ label: "朴素基线", ratio: asNumber(peak["theirs_at_1"]) }}
                  />
                </div>
                <div>
                  <div className="row" style={{ marginBottom: "var(--space-3)" }}>
                    <span className="mono" style={{ color: "var(--text-primary)" }}>
                      earliest anomaly
                    </span>
                    <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
                      最早异常点就是根因？基线弃权 {asText(earliest["abstained"])} 起
                    </span>
                  </div>
                  <CompareBars
                    ours={{ label: "本系统", ratio: asNumber(earliest["ours_at_1"]) }}
                    theirs={{ label: "朴素基线", ratio: asNumber(earliest["theirs_at_1"]) }}
                  />
                </div>
                <p className="section__note">
                  朴素基线在「最早异常」上放弃得比本系统多得多（{asText(earliest["abstained"])}{" "}
                  vs {asText(peak["abstained"])}）。放弃也是一种能力——
                  在证据不足时给出一个错误答案，比不给答案更贵。
                </p>
              </div>
            </Panel>
          </section>

          {/* evidence quality */}
          <section className="section">
            <div className="section__head">
              <span className="label">证据质量</span>
              <div className="section__rule" />
            </div>
            <Panel>
              <div className="stack">
                <BarRow
                  label="证据召回"
                  ratio={agg["evidence_recall_mean"] ?? null}
                  note="该引的都引了"
                  tone="supported"
                />
                <BarRow
                  label="引用精确率"
                  ratio={agg["citation_precision_mean"] ?? null}
                  note="引的都对"
                  tone="accent"
                />
                <BarRow
                  label="报告证据精确率"
                  ratio={agg["report_evidence_precision_mean"] ?? null}
                  note="报告正文引用的证据"
                  tone="info"
                />
                <BarRow
                  label="必须识别率"
                  ratio={agg["must_identify_rate"] ?? null}
                  note="注入的故障有没有被找到"
                  tone="info"
                />
              </div>
            </Panel>
          </section>
        </div>

        <div className="stack">
          {/* funnel */}
          <section className="section">
            <div className="section__head">
              <span className="label">真值漏斗</span>
              <div className="section__rule" />
            </div>
            <Panel>
              <div className="stack">
                <BarRow
                  label="真值被观测到"
                  ratio={data.funnel["observed"] ?? null}
                  tone="info"
                />
                <BarRow
                  label="进入候选池"
                  ratio={data.funnel["in_candidates"] ?? null}
                  tone="info"
                />
                <BarRow label="排到第一" ratio={data.funnel["top1"] ?? null} tone="accent" />
              </div>
              <p className="section__note">
                真值能被观测到，才能成为候选；成为候选，才可能被排到第一。
                每一段的损失都指向不同的修复方向。
              </p>
            </Panel>
          </section>

          {/* ablation arms */}
          <section className="section">
            <div className="section__head">
              <span className="label">消融实验</span>
              <div className="section__rule" />
              {data.runnableArms.length > 0 && (
                <span className="section__meta">可运行 {data.runnableArms.join(" / ")}</span>
              )}
            </div>
            <Panel>
              <div className="arms">
                {armIds.map((id) => {
                  const digest = data.arms[id]?.digest ?? "";
                  const results = data.armResults[id] ?? [];
                  // The rubric counts a correct abstention as a correct
                  // diagnosis, so `correct` includes abstentions. Keeping the
                  // two apart is what makes the pair readable: correctness
                  // over everything answered, and the answer rate beside it.
                  const decided = results.filter((r) => r["abstained"] !== true).length;
                  const answeredCorrectly = results.filter(
                    (r) => r["abstained"] !== true && r["diagnosis_correct"] === true,
                  ).length;
                  const abstained = results.length - decided;
                  const top1 = results.filter((r) => r["ac1"] === true).length;
                  return (
                    <button
                      key={id}
                      type="button"
                      className={`arm${selectedArm === id ? " is-active" : ""}`}
                      onClick={() => setArm(id)}
                      aria-pressed={selectedArm === id}
                    >
                      <div className="arm__id">
                        {id}
                        {data.runnableArms.includes(id) ? (
                          <span style={{ marginLeft: 6 }}>
                            <Badge tone="success">可运行</Badge>
                          </span>
                        ) : (
                          <span style={{ marginLeft: 6 }}>
                            <Badge tone="neutral">仅存档</Badge>
                          </span>
                        )}
                      </div>
                      <div className="arm__metric">
                        <span className="arm__value">{results.length}</span>
                        <span className="arm__label">场景</span>
                      </div>
                      <div className="arm__metric" style={{ marginTop: 4 }}>
                        <span className="arm__value" style={{ fontSize: "var(--text-md)" }}>
                          {answeredCorrectly}/{decided}
                        </span>
                        <span className="arm__label">判定正确 / 已判定</span>
                      </div>
                      <div className="arm__metric" style={{ marginTop: 4 }}>
                        <span className="arm__value" style={{ fontSize: "var(--text-md)" }}>
                          {top1}
                        </span>
                        <span className="arm__label">Top-1 命中</span>
                      </div>
                      {abstained > 0 && (
                        <div className="arm__metric" style={{ marginTop: 4 }}>
                          <span className="arm__value" style={{ fontSize: "var(--text-md)" }}>
                            {abstained}
                          </span>
                          <span className="arm__label">弃权</span>
                        </div>
                      )}
                      <div className="arm__digest">
                        <Hash value={digest} />
                      </div>
                    </button>
                  );
                })}
              </div>
              <p className="section__note">
                每个臂带一个配置摘要（digest）。E1/E2 已有归档但当前不可运行——
                消融只有在同一配置下重跑才可比。
              </p>
            </Panel>
          </section>

          {/* per-arm scenarios */}
          {selectedArm && (
            <section className="section">
              <div className="section__head">
                <span className="label">{selectedArm} 逐场景</span>
                <div className="section__rule" />
              </div>
              <Panel className="panel--flush">
                {armResults.length === 0 ? (
                  <EmptyState
                    title="这个臂没有结果"
                    detail="该配置只有 digest 存档，没有逐场景输出。"
                  />
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>场景</th>
                          <th style={{ width: 70 }}>服务</th>
                          <th style={{ width: 70 }}>故障</th>
                          <th style={{ width: 90 }}>判定</th>
                          <th style={{ width: 76 }}>名次</th>
                        </tr>
                      </thead>
                      <tbody>
                        {armResults.map((r) => {
                          const correct = r["diagnosis_correct"] === true;
                          const abstained = r["abstained"] === true;
                          return (
                            <tr key={String(r["scenario_id"])}>
                              <td className="cell-primary mono">
                                {String(r["scenario_id"] ?? "—")}
                              </td>
                              <td>
                                {r["ac1"] === true ? (
                                  <Badge tone="success">命中</Badge>
                                ) : (
                                  <span className="faint">—</span>
                                )}
                              </td>
                              <td>
                                {r["fault1"] === true ? (
                                  <Badge tone="success">命中</Badge>
                                ) : (
                                  <span className="faint">—</span>
                                )}
                              </td>
                              <td>
                                {abstained ? (
                                  <Badge tone="info">弃权</Badge>
                                ) : correct ? (
                                  <Badge tone="success">正确</Badge>
                                ) : (
                                  <Badge tone="critical">错误</Badge>
                                )}
                              </td>
                              <td className="cell-num">
                                {r["ranked"] === null || r["ranked"] === undefined
                                  ? "—"
                                  : String(r["ranked"])}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            </section>
          )}
        </div>
      </div>

      {/* all scenarios from latest.json */}
      <section className="section">
        <div className="section__head">
          <span className="label">全部场景</span>
          <div className="section__rule" />
          <span className="section__meta">{scenarios.length} 条</span>
        </div>
        <Panel className="panel--flush">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>场景</th>
                  <th style={{ width: 78 }}>结论</th>
                  <th style={{ width: 78 }}>弃权</th>
                  <th style={{ width: 88 }}>证据召回</th>
                  <th style={{ width: 92 }}>引用精确</th>
                  <th style={{ width: 72 }}>工具调用</th>
                  <th style={{ width: 86 }}>真值名次</th>
                  <th>缺失识别</th>
                </tr>
              </thead>
              <tbody>
                {scenarios.map((s, i) => {
                  const abstained = s["abstained"] === true;
                  const missing = Array.isArray(s["missing_identifications"])
                    ? (s["missing_identifications"] as unknown[])
                    : [];
                  const wrong = Array.isArray(s["wrong_answers_asserted"])
                    ? (s["wrong_answers_asserted"] as unknown[])
                    : [];
                  return (
                    <tr key={String(s["scenario_id"] ?? i)}>
                      <td className="cell-primary mono">{String(s["scenario_id"] ?? i)}</td>
                      <td>
                        {s["diagnosis_correct"] === true ? (
                          <Badge tone="success">正确</Badge>
                        ) : (
                          <Badge tone="critical">错误</Badge>
                        )}
                      </td>
                      <td>
                        {abstained ? (
                          <Badge tone="info">是</Badge>
                        ) : (
                          <span className="faint">否</span>
                        )}
                      </td>
                      <td className="cell-num">
                        {formatPercent(asNumber(s["evidence_recall"]))}
                      </td>
                      <td className="cell-num">
                        {formatPercent(asNumber(s["citation_precision"]))}
                      </td>
                      <td className="cell-num">{String(s["tool_calls"] ?? "—")}</td>
                      <td className="cell-num">
                        {s["ground_truth_rank"] === null || s["ground_truth_rank"] === undefined
                          ? "—"
                          : String(s["ground_truth_rank"])}
                      </td>
                      <td className="faint" style={{ fontSize: "var(--text-xs)" }}>
                        {missing.length > 0
                          ? missing.map((m) => String(m)).join(", ")
                          : wrong.length > 0
                            ? `误判：${wrong.map((m) => String(m)).join(", ")}`
                            : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      </section>
    </>
  );
}

function PageHead({ title, sub, note }: { title: string; sub: string; note?: string | null }) {
  return (
    <div className="page-head">
      <div className="page-head__text">
        <div className="eyebrow">
          <span className="label">评测</span>
        </div>
        <h1 className="page-title">{title}</h1>
        <p className="page-sub">{sub}</p>
        {note && (
          <p className="section__note" style={{ marginTop: "var(--space-3)" }}>
            <span className="mono">{note}</span>
          </p>
        )}
      </div>
    </div>
  );
}

function asRecord(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function asText(v: unknown): string {
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  if (typeof v === "string" && v.length > 0) return v;
  return "—";
}
