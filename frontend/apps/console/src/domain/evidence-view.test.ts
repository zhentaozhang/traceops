/**
 * 证据载荷解析器的测试。
 *
 * `normalized_data` 是按查询模板分形状的联合类型，但载荷里**没有任何字段
 * 说明自己是哪一种**——所以解析器只能靠嗅探形状。嗅探错了的代价不对称：
 * 把日志当指标渲染出来，看起来像有数据，其实是在骗人。
 *
 * 所以这里重点测两件事：
 *   1. 认得的形状要认对
 *   2. 认不得的形状要返回空/null，**绝不猜**
 */
import { describe, expect, it } from "vitest";
import {
  eventRecordsOf,
  logRecordsOf,
  metricSeriesOf,
  payloadShapeOf,
  payloadTotal,
  spanRecordsOf,
} from "./evidence-view";

describe("metricSeriesOf", () => {
  it("解析 Prometheus matrix，把 [unix秒, 值] 转成点", () => {
    const series = metricSeriesOf({
      result_type: "matrix",
      series: [
        {
          metric: { service: "checkout-service" },
          values: [
            [1_700_000_000, 0.05],
            [1_700_000_060, 0.9],
          ],
        },
      ],
    });
    expect(series).toHaveLength(1);
    expect(series[0]?.service).toBe("checkout-service");
    expect(series[0]?.points).toEqual([
      { t: 1_700_000_000, v: 0.05 },
      { t: 1_700_000_060, v: 0.9 },
    ]);
  });

  it("按时间排序，不管输入顺序", () => {
    const series = metricSeriesOf({
      series: [
        {
          metric: {},
          values: [
            [200, 2],
            [100, 1],
          ],
        },
      ],
    });
    expect(series[0]?.points.map((p) => p.t)).toEqual([100, 200]);
  });

  it("丢掉残缺的点，而不是当成 0", () => {
    const series = metricSeriesOf({
      series: [{ metric: {}, values: [[100], [100, 1], ["x", 2], [200, "y"]] }],
    });
    expect(series[0]?.points).toEqual([{ t: 100, v: 1 }]);
  });

  it("识别不了载荷时返回空数组", () => {
    expect(metricSeriesOf({ kind: "logs", records: [] })).toEqual([]);
    expect(metricSeriesOf("nonsense")).toEqual([]);
    expect(metricSeriesOf(null)).toEqual([]);
  });

  it("service 缺失时是 null，不是空字符串", () => {
    const series = metricSeriesOf({ series: [{ values: [] }] });
    expect(series[0]?.service).toBeNull();
  });

  it("透传 total_records 与 truncated", () => {
    const series = metricSeriesOf({
      series: [{ metric: {}, values: [] }],
      total_records: 4210,
      truncated: true,
    });
    expect(series[0]?.totalRecords).toBe(4210);
    expect(series[0]?.truncated).toBe(true);
  });
});

describe("logRecordsOf", () => {
  it("解析日志记录", () => {
    const records = logRecordsOf({
      kind: "logs",
      records: [
        {
          timestamp: "2026-03-12T14:32:00Z",
          message: "connection pool exhausted",
          level: "ERROR",
          labels: { service: "payment-service" },
          trace_id: "t-1",
          span_id: "s-1",
        },
      ],
    });
    expect(records).toHaveLength(1);
    expect(records[0]?.message).toBe("connection pool exhausted");
    expect(records[0]?.level).toBe("ERROR");
    expect(records[0]?.traceId).toBe("t-1");
    expect(records[0]?.labels).toEqual({ service: "payment-service" });
  });

  it('labels 里的非字符串值被丢掉，不会变成 "undefined"', () => {
    const records = logRecordsOf({
      records: [{ message: "m", labels: { ok: "yes", bad: 42, nested: { a: 1 } } }],
    });
    expect(records[0]?.labels).toEqual({ ok: "yes" });
  });

  it("认不得时返回空数组", () => {
    expect(logRecordsOf({ kind: "traces", spans: [] })).toEqual([]);
    expect(logRecordsOf(undefined)).toEqual([]);
  });
});

describe("spanRecordsOf", () => {
  it("解析 span，微秒时间戳转成 ISO", () => {
    const spans = spanRecordsOf({
      kind: "traces",
      spans: [
        {
          trace_id: "t-1",
          span_id: "s-1",
          service: "checkout-service",
          operation_name: "POST /checkout",
          start_time: 1_700_000_000_000_000,
          duration_us: 1_500_000,
          error: true,
        },
      ],
    });
    expect(spans).toHaveLength(1);
    expect(spans[0]?.operation).toBe("POST /checkout");
    expect(spans[0]?.startTime).toBe("2023-11-14T22:13:20.000Z");
    expect(spans[0]?.durationUs).toBe(1_500_000);
    expect(spans[0]?.error).toBe(true);
  });

  it("error 可以是布尔也可以是字符串", () => {
    expect(spanRecordsOf({ spans: [{ error: "timeout" }] })[0]?.error).toBe("timeout");
    expect(spanRecordsOf({ spans: [{ error: false }] })[0]?.error).toBe(false);
    expect(spanRecordsOf({ spans: [{}] })[0]?.error).toBeNull();
  });

  it("start_time 不是数字时按字符串原样保留", () => {
    const spans = spanRecordsOf({ spans: [{ start_time: "2026-03-12T14:32:00Z" }] });
    expect(spans[0]?.startTime).toBe("2026-03-12T14:32:00Z");
  });

  it("认不得时返回空数组", () => {
    expect(spanRecordsOf({ kind: "logs", records: [] })).toEqual([]);
  });
});

describe("eventRecordsOf", () => {
  it("解析嵌套的 event 包装", () => {
    const events = eventRecordsOf({
      kind: "events",
      events: [
        {
          event: {
            changed_at: "2026-03-12T14:30:00Z",
            change_kind: "deployment",
            service: "checkout-service",
            revision: "abc123",
            summary: "rollout v2.3.1",
            type: "config",
          },
        },
      ],
    });
    expect(events).toHaveLength(1);
    expect(events[0]?.changedAt).toBe("2026-03-12T14:30:00Z");
    expect(events[0]?.changeKind).toBe("deployment");
    expect(events[0]?.revision).toBe("abc123");
  });

  it("没有 event 包装时读平铺字段", () => {
    const events = eventRecordsOf({ events: [{ changed_at: "2026-03-12T14:30:00Z" }] });
    expect(events[0]?.changedAt).toBe("2026-03-12T14:30:00Z");
  });

  it("认不得时返回空数组", () => {
    expect(eventRecordsOf({ kind: "logs" })).toEqual([]);
  });
});

describe("payloadShapeOf", () => {
  it("按 kind 识别", () => {
    expect(payloadShapeOf({ kind: "logs", records: [] })).toBe("logs");
    expect(payloadShapeOf({ kind: "traces", spans: [] })).toBe("traces");
    expect(payloadShapeOf({ kind: "events", events: [] })).toBe("events");
  });

  it("Prometheus matrix 靠 result_type 识别", () => {
    expect(payloadShapeOf({ result_type: "matrix", series: [] })).toBe("matrix");
  });

  it("认不得的 kind 是 unknown，不会硬套成某一种", () => {
    expect(payloadShapeOf({ kind: "sparkline" })).toBe("unknown");
    expect(payloadShapeOf({ kind: "logs" })).toBe("unknown");
  });

  it("空载荷是 unknown", () => {
    expect(payloadShapeOf({})).toBe("unknown");
    expect(payloadShapeOf(null)).toBe("unknown");
    expect(payloadShapeOf("text")).toBe("unknown");
  });
});

describe("payloadTotal", () => {
  it("读 snake_case 与 camelCase 两种写法", () => {
    expect(payloadTotal({ total_records: 12 })).toBe(12);
    expect(payloadTotal({ totalRecords: 34 })).toBe(34);
  });

  it("没声明总数时是 null", () => {
    expect(payloadTotal({ kind: "logs" })).toBeNull();
    expect(payloadTotal(null)).toBeNull();
  });
});
