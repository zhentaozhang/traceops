/**
 * 格式化层的测试。
 *
 * 这一层是纯函数，测起来最便宜，而且它决定了值班的人每天看到的每一个数字。
 * 两个重点：
 *   1. 缺失值一律是 "—"，不能变成 "NaN" / "undefined" / "Invalid Date"
 *   2. 时区固定 Asia/Shanghai，原始值保留 UTC（README 约定）
 */
import { describe, expect, it } from "vitest";
import {
  EVIDENCE_TYPE_LABELS,
  evidenceTypeLabel,
  faultLabel,
  formatCount,
  formatDuration,
  formatInstant,
  formatInstantShort,
  formatPercent,
  formatRelative,
  formatTokens,
  formatWindow,
  LANE_LABELS,
  SEVERITY_LABELS,
  STAGE_LABELS,
  shortHash,
  shortId,
  VERDICT_LABELS,
  WAITING_ON_LABELS,
} from "./format";

describe("formatInstant", () => {
  it("按 Asia/Shanghai 渲染", () => {
    // UTC 06:32 → 上海 14:32
    expect(formatInstant("2026-03-12T06:32:00Z")).toBe("2026-03-12 14:32:00");
  });

  it("缺失或非法输入是破折号", () => {
    expect(formatInstant(null)).toBe("—");
    expect(formatInstant(undefined)).toBe("—");
    expect(formatInstant("")).toBe("—");
    expect(formatInstant("not-a-date")).toBe("—");
  });
});

describe("formatInstantShort", () => {
  it("省略年份", () => {
    expect(formatInstantShort("2026-03-12T06:32:00Z")).toBe("03-12 14:32");
  });

  it("缺失时是破折号", () => {
    expect(formatInstantShort(null)).toBe("—");
  });
});

describe("formatRelative", () => {
  it("45 秒内是「刚刚」", () => {
    const now = new Date();
    expect(formatRelative(now.toISOString())).toBe("刚刚");
  });

  it("分钟级用「X 分钟前」", () => {
    const t = new Date(Date.now() - 5 * 60_000).toISOString();
    expect(formatRelative(t)).toBe("5 分钟前");
  });

  it("小时级用「X 小时前」", () => {
    const t = new Date(Date.now() - 3 * 3_600_000).toISOString();
    expect(formatRelative(t)).toBe("3 小时前");
  });

  it("天级用「X 天前」", () => {
    const t = new Date(Date.now() - 10 * 86_400_000).toISOString();
    expect(formatRelative(t)).toBe("10 天前");
  });

  it("未来时间说「后」", () => {
    const t = new Date(Date.now() + 5 * 60_000).toISOString();
    expect(formatRelative(t)).toBe("5 分钟后");
  });

  it("缺失时是破折号", () => {
    expect(formatRelative(null)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("毫秒 / 秒 / 分秒三级", () => {
    expect(formatDuration(310)).toBe("310ms");
    expect(formatDuration(1_400)).toBe("1.4s");
    expect(formatDuration(125_000)).toBe("2m 05s");
  });

  it("缺失或非法输入是破折号", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("formatCount", () => {
  it("千 / 百万 / 十亿分级", () => {
    expect(formatCount(999)).toBe("999");
    expect(formatCount(1_200)).toBe("1.2k");
    expect(formatCount(3_400_000)).toBe("3.4M");
    expect(formatCount(2_100_000_000)).toBe("2.1G");
  });

  it("负数照样分级", () => {
    expect(formatCount(-1_200)).toBe("-1.2k");
  });

  it("缺失或非法输入是破折号", () => {
    expect(formatCount(null)).toBe("—");
    expect(formatCount(Number.NaN)).toBe("—");
  });
});

describe("formatTokens / formatPercent", () => {
  it("token 数走 formatCount", () => {
    expect(formatTokens(1_500)).toBe("1.5k");
    expect(formatTokens(null)).toBe("—");
  });

  it("百分比是 0..1 的比值", () => {
    expect(formatPercent(0.923)).toBe("92.3%");
    expect(formatPercent(1)).toBe("100.0%");
    expect(formatPercent(0.5, 0)).toBe("50%");
  });

  it("缺失时是破折号", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(undefined)).toBe("—");
  });
});

describe("formatWindow", () => {
  it("两端都有时给区间", () => {
    expect(formatWindow("2026-03-12T06:22:00Z", "2026-03-12T06:37:00Z")).toBe(
      "03-12 14:22 → 03-12 14:37",
    );
  });

  it("只有一端时只显示那一端", () => {
    expect(formatWindow("2026-03-12T06:22:00Z", null)).toBe("03-12 14:22");
    expect(formatWindow(null, "2026-03-12T06:37:00Z")).toBe("03-12 14:37");
  });

  it("两端都没有时是破折号", () => {
    expect(formatWindow(null, null)).toBe("—");
  });
});

describe("shortId / shortHash", () => {
  it("长 id 缩成 头…尾", () => {
    expect(shortId("e-9310bce3f4a2b1c8d7e6f5a4b3c2d1e4c1e")).toBe("e-9310bce…4c1e");
  });

  it("短 id 原样返回", () => {
    expect(shortId("abc")).toBe("abc");
    expect(shortId(null)).toBe("—");
  });

  it("checksum 截前 12 位", () => {
    expect(shortHash("sha256:0123456789abcdef")).toBe("sha256:01234");
    expect(shortHash(null)).toBe("—");
  });
});

describe("标签表", () => {
  it("故障类型有中文标签，未知的退化成去下划线的原文", () => {
    expect(faultLabel("pool_exhaustion")).toBe("连接池耗尽");
    expect(faultLabel("some_new_fault")).toBe("some new fault");
    expect(faultLabel(null)).toBe("—");
  });

  it("证据类型有中文标签，未知的原样返回", () => {
    expect(evidenceTypeLabel("metric")).toBe("指标");
    expect(evidenceTypeLabel("new_kind")).toBe("new_kind");
    expect(evidenceTypeLabel(null)).toBe("—");
  });

  it("所有枚举标签表都覆盖 UNKNOWN", () => {
    expect(STAGE_LABELS.UNKNOWN).toBe("未知");
    expect(WAITING_ON_LABELS.UNKNOWN).toBe("未知");
    expect(SEVERITY_LABELS.UNKNOWN).toBe("未知");
    expect(VERDICT_LABELS.UNKNOWN).toBe("结论缺失");
  });

  it("lane 标签覆盖五个 lane", () => {
    for (const lane of ["waiting-person", "waiting-clock", "active", "finished", "failed"]) {
      expect(LANE_LABELS[lane]).toBeTruthy();
    }
  });

  it("证据类型表覆盖载荷解析器认识的全部类型", () => {
    for (const t of ["metric", "log", "trace", "event", "relation", "deployment", "topology"]) {
      expect(EVIDENCE_TYPE_LABELS[t]).toBeTruthy();
    }
  });
});
