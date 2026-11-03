/**
 * 适配器层的硬约束测试。
 *
 * 这三条约束写在前端 README 里，是「后端怎么改，前端都不散架」的保证：
 *   1. 永不抛异常
 *   2. 未知枚举保留原值（raw*）
 *   3. 缺失的可选数据是 null，不是看起来像真的占位符
 *
 * 所以这里测的不是「正常输入能不能解析」，而是「畸形输入会不会炸、会不会
 * 悄悄变成看起来正常的东西」。
 */
import { describe, expect, it } from "vitest";
import {
  laneOf,
  toEvidenceRefVM,
  toHealthVM,
  toIncidentVM,
  toReportVM,
  toRunVM,
  urgencyOf,
} from "./adapters";

/** 一组足够畸形的东西，用来证明适配器不会炸。 */
const MALFORMED: unknown[] = [
  undefined,
  null,
  0,
  "",
  "string",
  42,
  true,
  [],
  [1, 2, 3],
  {},
  { id: null, service: 123, title: [], stage: "not-an-object" },
  { stage: { kind: 42, waitingOn: [] } },
  { severity: { nested: "object" } },
  { window: "not-an-object" },
  { hypothesis_evidence: "not-an-array" },
  { hypotheses: [null, 1, "x", {}] },
  { propagation_chain: [null, [], ["only-one"], ["a", "b", "c"]] },
  { rejected_proposals: [null, {}, { proposal: "p" }] },
  { shadow_decisions: [null, {}, { note: "no candidate" }] },
  { components: [null, 1, "x", { status: 42 }] },
  { funnel: "not-an-object" },
];

describe("toIncidentVM", () => {
  it("对畸形输入永不抛异常", () => {
    for (const input of MALFORMED) {
      expect(() => toIncidentVM(input)).not.toThrow();
    }
  });

  it("非对象输入退化成安全的空事故，而不是白屏", () => {
    const vm = toIncidentVM("nonsense");
    expect(vm.id).toBe("—");
    expect(vm.service).toBe("—");
    expect(vm.title).toBe("（无标题）");
    expect(vm.stage).toBeNull();
  });

  it("未知 severity 保留原值并降级为 UNKNOWN", () => {
    const vm = toIncidentVM({ severity: "catastrophic" });
    expect(vm.severity).toBe("UNKNOWN");
    expect(vm.rawSeverity).toBe("catastrophic");
  });

  it("severity 大小写不敏感", () => {
    expect(toIncidentVM({ severity: "critical" }).severity).toBe("CRITICAL");
  });

  it("未知 stage kind / waitingOn 保留原值", () => {
    const vm = toIncidentVM({
      stage: { kind: "ESCALATED", waitingOn: "ROBOT", detail: "d", retryable: true },
    });
    expect(vm.stage?.kind).toBe("UNKNOWN");
    expect(vm.stage?.rawKind).toBe("ESCALATED");
    expect(vm.stage?.waitingOn).toBe("UNKNOWN");
    expect(vm.stage?.rawWaitingOn).toBe("ROBOT");
    expect(vm.stage?.retryable).toBe(true);
  });

  it("retryable 只在严格 true 时成立", () => {
    expect(
      toIncidentVM({ stage: { kind: "DONE", waitingOn: "NOBODY", retryable: "yes" } }).stage
        ?.retryable,
    ).toBe(false);
    expect(
      toIncidentVM({ stage: { kind: "DONE", waitingOn: "NOBODY", retryable: true } }).stage
        ?.retryable,
    ).toBe(true);
  });

  it("缺失的 stage 是 null，不是一个假的「调查中」", () => {
    expect(toIncidentVM({ id: "x" }).stage).toBeNull();
  });

  it("confirmation 只接受 ACCEPTED / REJECTED，其余为 null", () => {
    expect(toIncidentVM({ confirmation: "ACCEPTED" }).confirmation).toBe("ACCEPTED");
    expect(toIncidentVM({ confirmation: "REJECTED" }).confirmation).toBe("REJECTED");
    expect(toIncidentVM({ confirmation: "MAYBE" }).confirmation).toBeNull();
    expect(toIncidentVM({}).confirmation).toBeNull();
  });

  it("空字符串不算有效值（退化成 null）", () => {
    const vm = toIncidentVM({ assignee: "", acknowledgedAt: "", currentRunId: "" });
    expect(vm.assignee).toBeNull();
    expect(vm.acknowledgedAt).toBeNull();
    expect(vm.currentRunId).toBeNull();
  });

  it("同时接受 snake_case 和 camelCase 的窗口字段", () => {
    expect(toIncidentVM({ windowStart: "2026-01-01T00:00:00Z" }).windowStart).toBe(
      "2026-01-01T00:00:00Z",
    );
    expect(
      toIncidentVM({ window: { start: "2026-01-01T00:00:00Z", end: "2026-01-01T01:00:00Z" } })
        .windowEnd,
    ).toBe("2026-01-01T01:00:00Z");
  });
});

describe("laneOf / urgencyOf", () => {
  it("同一个 kind 靠 waitingOn 分到不同 lane", () => {
    // 这是这个小模型存在的全部理由
    expect(laneOf("RUNNING", "PERSON")).toBe("waiting-person");
    expect(laneOf("RUNNING", "CLOCK")).toBe("waiting-clock");
    expect(laneOf("RUNNING", "WORKER")).toBe("active");
    expect(laneOf("RUNNING", "NOBODY")).toBe("active");
  });

  it("DONE / DEAD 优先于 waitingOn", () => {
    expect(laneOf("DONE", "PERSON")).toBe("finished");
    expect(laneOf("DEAD", "PERSON")).toBe("failed");
  });

  it("NEEDS_DECISION 一律待人工", () => {
    expect(laneOf("NEEDS_DECISION", "WORKER")).toBe("waiting-person");
  });

  it("RETRY_BACKOFF / QUEUED 归待重试", () => {
    expect(laneOf("RETRY_BACKOFF", "WORKER")).toBe("waiting-clock");
    expect(laneOf("QUEUED", "WORKER")).toBe("waiting-clock");
  });

  it("紧急度排序：待人工 > 已失败 > 进行中 > 待重试 > 已完成", () => {
    const w = (
      kind: "DONE" | "DEAD" | "RUNNING" | "QUEUED",
      waitingOn: "PERSON" | "CLOCK" | "WORKER" | "NOBODY",
    ) => urgencyOf({ lane: laneOf(kind, waitingOn) } as never);
    expect(w("RUNNING", "PERSON")).toBeLessThan(w("DEAD", "NOBODY"));
    expect(w("DEAD", "NOBODY")).toBeLessThan(w("RUNNING", "WORKER"));
    expect(w("RUNNING", "WORKER")).toBeLessThan(w("QUEUED", "CLOCK"));
    expect(w("QUEUED", "CLOCK")).toBeLessThan(w("DONE", "NOBODY"));
  });

  it("没有 stage 的事故排在中间而不是最前", () => {
    expect(urgencyOf(null)).toBe(50);
  });
});

describe("toReportVM", () => {
  it("对畸形输入永不抛异常", () => {
    for (const input of MALFORMED) {
      expect(() => toReportVM(input)).not.toThrow();
    }
  });

  it("从 hypothesis_evidence 数出每个假设的支持与反驳数", () => {
    const vm = toReportVM({
      investigation_run_id: "run-1",
      hypothesis_evidence: [
        { hypothesis_id: "h1", relation_type: "SUPPORTS" },
        { hypothesis_id: "h1", relation_type: "SUPPORTS" },
        { hypothesis_id: "h1", relation_type: "REFUTES" },
        { hypothesis_id: "h2", relation_type: "REFUTES" },
      ],
      hypotheses: [{ id: "h1" }, { id: "h2" }],
    });
    expect(vm.hypotheses[0]?.supportCount).toBe(2);
    expect(vm.hypotheses[0]?.refuteCount).toBe(1);
    expect(vm.hypotheses[1]?.supportCount).toBe(0);
    expect(vm.hypotheses[1]?.refuteCount).toBe(1);
  });

  it("missing_data 与 no_data 分开保存（前者不构成反驳）", () => {
    const vm = toReportVM({
      missing_data: ["没有部署事件"],
      no_data: ["payment-service 没有错误日志"],
    });
    expect(vm.missingData).toEqual(["没有部署事件"]);
    expect(vm.noData).toEqual(["payment-service 没有错误日志"]);
  });

  it("propagation_chain 同时接受二元组与 from/to 对象", () => {
    const vm = toReportVM({
      propagation_chain: [
        ["checkout-service", "payment-service"],
        { from: "payment-service", to: "redis" },
        { source: "a", target: "b" },
      ],
    });
    expect(vm.propagationChain).toEqual([
      ["checkout-service", "payment-service"],
      ["payment-service", "redis"],
      ["a", "b"],
    ]);
  });

  it("残缺的 propagation_chain 边被丢弃，不产生半条边", () => {
    const vm = toReportVM({
      propagation_chain: [["only-one"], [null, "b"], ["a", null], ["c", "d"]],
    });
    expect(vm.propagationChain).toEqual([["c", "d"]]);
  });

  it("未知 verdict 降级为 UNKNOWN 并保留原值", () => {
    const vm = toReportVM({ diagnosis_status: "PROBABLY_FINE" });
    expect(vm.verdict).toBe("UNKNOWN");
    expect(vm.rawVerdict).toBe("PROBABLY_FINE");
  });

  it("abstained 是独立标记，不会被 verdict 覆盖", () => {
    const vm = toReportVM({ abstained: true, diagnosis_status: "INCONCLUSIVE" });
    expect(vm.abstained).toBe(true);
    expect(vm.verdict).toBe("INCONCLUSIVE");
  });

  it("rejected_proposals 丢掉既无提案也无原因码的项", () => {
    const vm = toReportVM({
      rejected_proposals: [
        { proposal: "p1", reason_code: "NO_EVIDENCE" },
        { detail: "只有细节" },
        null,
      ],
    });
    expect(vm.rejectedProposals).toHaveLength(1);
    expect(vm.rejectedProposals[0]?.reasonCode).toBe("NO_EVIDENCE");
  });

  it("shadow_decisions 丢掉没有候选的项", () => {
    const vm = toReportVM({
      shadow_decisions: [{ candidate: "redis", note: "n" }, { note: "x" }],
    });
    expect(vm.shadowDecisions).toHaveLength(1);
    expect(vm.shadowDecisions[0]?.candidate).toBe("redis");
  });

  it("topology_drift 只保留计数，不保留原始边", () => {
    const vm = toReportVM({
      topology_drift: {
        declared_edges: [["a", "b"]],
        observed_edges: [["b", "c"]],
        undeclared_edges: [],
      },
    });
    expect(vm.topologyDrift).toEqual({ declared: 1, observed: 1, undeclared: 0 });
  });

  it("没有 topology_drift 时是 null，不是空计数", () => {
    expect(toReportVM({}).topologyDrift).toBeNull();
  });

  it("成本从 cost 块或顶层字段读，两处都行", () => {
    expect(toReportVM({ cost: { toolCalls: 18, tokens: 1200 } }).cost).toEqual({
      toolCalls: 18,
      tokens: 1200,
    });
    expect(toReportVM({ tool_calls: 7, tokens_used: 300 }).cost).toEqual({
      toolCalls: 7,
      tokens: 300,
    });
  });

  it("成本缺失时是 null，不是 0（0 会被误读成「没花代价」）", () => {
    expect(toReportVM({}).cost).toEqual({ toolCalls: null, tokens: null });
  });
});

describe("toEvidenceRefVM", () => {
  it("对畸形输入永不抛异常", () => {
    for (const input of MALFORMED) {
      expect(() => toEvidenceRefVM(input)).not.toThrow();
    }
  });

  it("snake_case 与 camelCase 都读", () => {
    const vm = toEvidenceRefVM({
      evidence_type: "metric",
      service_id: "checkout-service",
      observed_at: "2026-03-12T14:32:00Z",
      query_template_id: "qt-error-rate-v1",
    });
    expect(vm.evidenceType).toBe("metric");
    expect(vm.serviceId).toBe("checkout-service");
    expect(vm.observedAt).toBe("2026-03-12T14:32:00Z");
    expect(vm.queryTemplateId).toBe("qt-error-rate-v1");
  });

  it("缺失的来源链接是 null", () => {
    expect(toEvidenceRefVM({ id: "e1" }).sourceLink).toBeNull();
  });
});

describe("toRunVM", () => {
  it("对畸形输入永不抛异常", () => {
    for (const input of MALFORMED) {
      expect(() => toRunVM(input)).not.toThrow();
    }
  });

  it("current 指向最后一步", () => {
    const vm = toRunVM({
      runId: "r1",
      steps: [
        { stepIndex: 1, node: "plan" },
        { stepIndex: 2, node: "collect" },
      ],
    });
    expect(vm.current?.node).toBe("collect");
    expect(vm.steps).toHaveLength(2);
  });

  it("没有步骤时 current 是 null", () => {
    expect(toRunVM({ runId: "r1", steps: [] }).current).toBeNull();
  });
});

describe("toHealthVM", () => {
  it("对畸形输入永不抛异常", () => {
    for (const input of MALFORMED) {
      expect(() => toHealthVM(input)).not.toThrow();
    }
  });

  it("未知组件状态降级为 UNKNOWN", () => {
    const vm = toHealthVM({ components: [{ name: "redis", status: "flapping" }] });
    expect(vm.components[0]?.status).toBe("UNKNOWN");
  });

  it("漏斗字段齐全，缺一个补 0", () => {
    const vm = toHealthVM({ funnel: { alertsSeen: 10, runsRequested: 4 } });
    expect(vm.funnel?.alertsSeen).toBe(10);
    expect(vm.funnel?.runsRequested).toBe(4);
    expect(vm.funnel?.runsFailed).toBe(0);
    expect(vm.funnel?.needsHumanReview).toBe(0);
  });

  it("没有漏斗时是 null", () => {
    expect(toHealthVM({}).funnel).toBeNull();
  });
});
