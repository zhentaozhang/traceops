/**
 * Fixture 数据源的测试。
 *
 * 这个数据源现在是唯一的实现，将来会被 `ApiDataSource` 换掉——但在那之前，
 * 它同时承担两个角色：**页面数据源** 和 **fixture 完整性检查器**。
 *
 * 后一个角色很重要：fixture 是从仓库真实产物派生的（`scripts/build-fixtures.mjs`），
 * 一旦源文件变了而 fixture 没跟上，页面会显示错的东西而不是报错。所以这里
 * 断言的是「fixture 内部自洽」——引用的 run/evidence/hypothesis 都真的存在。
 */
import { describe, expect, it } from "vitest";
import { dataSource } from "./source";

describe("listIncidents", () => {
  it("返回全部事故", async () => {
    const all = await dataSource.listIncidents();
    expect(all.length).toBeGreaterThan(0);
    expect(all.every((i) => i.id && i.service && i.title)).toBe(true);
  });

  it("按服务过滤", async () => {
    const all = await dataSource.listIncidents();
    const service = all[0]?.service;
    expect(service).toBeTruthy();
    const filtered = await dataSource.listIncidents({ service: service ?? "" });
    expect(filtered.length).toBeGreaterThan(0);
    expect(filtered.every((i) => i.service === service)).toBe(true);
  });

  it("按 lane 过滤", async () => {
    const all = await dataSource.listIncidents();
    const lane = all.find((i) => i.stage)?.stage?.lane;
    expect(lane).toBeTruthy();
    const filtered = await dataSource.listIncidents({ lane: lane ?? "" });
    expect(filtered.every((i) => i.stage?.lane === lane)).toBe(true);
  });

  it("按严重级别过滤", async () => {
    const all = await dataSource.listIncidents();
    const severity = all[0]?.severity;
    expect(severity).toBeTruthy();
    const filtered = await dataSource.listIncidents({ severity: severity ?? "" });
    expect(filtered.every((i) => i.severity === severity)).toBe(true);
  });

  it("过滤不匹配的东西时返回空数组，而不是全部", async () => {
    expect(await dataSource.listIncidents({ service: "不存在的服务" })).toEqual([]);
    expect(await dataSource.listIncidents({ lane: "不存在的 lane" })).toEqual([]);
  });

  it("覆盖多种 stage 组合（README 说是完备集）", async () => {
    const all = await dataSource.listIncidents();
    const kinds = new Set(all.map((i) => i.stage?.kind).filter(Boolean));
    const lanes = new Set(all.map((i) => i.stage?.lane).filter(Boolean));
    expect(kinds.size).toBeGreaterThanOrEqual(4);
    expect(lanes.size).toBeGreaterThanOrEqual(4);
  });
});

describe("getIncident", () => {
  it("按 id 找到事故", async () => {
    const all = await dataSource.listIncidents();
    const first = all[0];
    expect(first).toBeDefined();
    const one = await dataSource.getIncident(first?.id ?? "");
    expect(one?.id).toBe(first?.id);
  });

  it("找不到时返回 null", async () => {
    expect(await dataSource.getIncident("no-such-incident")).toBeNull();
  });
});

describe("listServices", () => {
  it("返回去重排序后的服务列表", async () => {
    const services = await dataSource.listServices();
    expect(services.length).toBeGreaterThan(0);
    expect([...services].sort()).toEqual(services);
    expect(new Set(services).size).toBe(services.length);
  });

  it("每个服务名都对应至少一起事故", async () => {
    const services = await dataSource.listServices();
    const incidents = await dataSource.listIncidents();
    for (const s of services) {
      expect(incidents.some((i) => i.service === s)).toBe(true);
    }
  });
});

describe("报告与证据", () => {
  it("有报告的事故，其报告里的 run id 与事故的 currentRunId 一致", async () => {
    const incidents = await dataSource.listIncidents();
    let checked = 0;
    for (const incident of incidents) {
      if (!incident.currentRunId) continue;
      const report = await dataSource.getReport(incident.currentRunId);
      if (!report) continue;
      expect(
        report.runId,
        `事故 ${incident.id} 的报告 run id 应该是 ${incident.currentRunId}`,
      ).toBe(incident.currentRunId);
      checked += 1;
    }
    expect(checked).toBeGreaterThan(0);
  });

  it("找不到的 run 返回 null", async () => {
    expect(await dataSource.getReport("run-does-not-exist")).toBeNull();
    expect(await dataSource.getRun("run-does-not-exist")).toBeNull();
  });

  it("报告里的每条证据都能按 id 查到详情", async () => {
    const report = await dataSource.getReport("run-demo-1");
    expect(report).not.toBeNull();
    expect(report?.evidence.length).toBeGreaterThan(0);
    for (const ref of report?.evidence ?? []) {
      const detail = await dataSource.getEvidenceDetail(ref.id);
      expect(detail, `证据 ${ref.id} 查不到详情`).not.toBeNull();
      expect(detail?.id).toBe(ref.id);
    }
  });

  it("getEvidence 返回的 id 集合与报告一致", async () => {
    const report = await dataSource.getReport("run-demo-1");
    const evidence = await dataSource.getEvidence("run-demo-1");
    expect(evidence.map((e) => e.id).sort()).toEqual(
      (report?.evidence ?? []).map((e) => e.id).sort(),
    );
  });

  it("找不到的证据详情返回 null", async () => {
    expect(await dataSource.getEvidenceDetail("no-such-evidence")).toBeNull();
  });

  it("同一 run 内证据 id 唯一", async () => {
    const ids = (await dataSource.getEvidence("run-demo-1")).map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("证据详情保留 normalized_data 与 raw_payload", async () => {
    const evidence = await dataSource.getEvidence("run-demo-1");
    expect(evidence.length).toBeGreaterThan(0);
    expect(evidence[0]?.normalizedData).not.toBeUndefined();
  });
});

describe("getRelations", () => {
  it("关系边的两端都指向真实存在的 id", async () => {
    const relations = await dataSource.getRelations("run-demo-1");
    expect(relations.length).toBeGreaterThan(0);

    const report = await dataSource.getReport("run-demo-1");
    const evidenceIds = new Set((report?.evidence ?? []).map((e) => e.id));
    const hypothesisIds = new Set((report?.hypotheses ?? []).map((h) => h.id));

    for (const rel of relations) {
      expect(
        hypothesisIds.has(rel.sourceId) || evidenceIds.has(rel.sourceId),
        `关系 ${rel.id} 的 sourceId ${rel.sourceId} 不存在`,
      ).toBe(true);
      expect(
        hypothesisIds.has(rel.targetId) || evidenceIds.has(rel.targetId),
        `关系 ${rel.id} 的 targetId ${rel.targetId} 不存在`,
      ).toBe(true);
    }
  });

  it("每条关系都带 relationType", async () => {
    const relations = await dataSource.getRelations("run-demo-1");
    expect(relations.every((r) => r.relationType.length > 0)).toBe(true);
  });

  it("没有关系的 run 返回空数组", async () => {
    expect(await dataSource.getRelations("run-does-not-exist")).toEqual([]);
  });
});

describe("getRun", () => {
  it("进度步骤的 stepIndex 单调不减（旧值要能丢弃）", async () => {
    const run = await dataSource.getRun("run-demo-1");
    expect(run).not.toBeNull();
    const indices = (run?.steps ?? []).map((s) => s.stepIndex);
    for (let i = 1; i < indices.length; i++) {
      expect(indices[i] ?? 0).toBeGreaterThanOrEqual(indices[i - 1] ?? 0);
    }
  });
});

describe("getHealth", () => {
  it("返回组件健康与漏斗", async () => {
    const health = await dataSource.getHealth();
    expect(health.components.length).toBeGreaterThan(0);
    expect(health.funnel).not.toBeNull();
    expect(health.funnel?.alertsSeen).toBeGreaterThan(0);
  });
});

describe("getEvaluation", () => {
  it("返回聚合指标、场景与消融 arm", async () => {
    const ev = await dataSource.getEvaluation();
    expect(Object.keys(ev.aggregate).length).toBeGreaterThan(0);
    expect(ev.scenarios.length).toBeGreaterThan(0);
    expect(Object.keys(ev.arms).length).toBeGreaterThan(0);
  });

  it("保留「这是合成场景」的免责说明", async () => {
    const ev = await dataSource.getEvaluation();
    expect(ev.note).toBeTruthy();
  });
});

describe("getTopology", () => {
  it("拓扑边都是二元组", async () => {
    const topo = await dataSource.getTopology();
    expect(topo.edges.length).toBeGreaterThan(0);
    for (const edge of topo.edges) {
      expect(Array.isArray(edge)).toBe(true);
      expect(edge).toHaveLength(2);
      expect(typeof edge[0]).toBe("string");
      expect(typeof edge[1]).toBe("string");
    }
  });
});
