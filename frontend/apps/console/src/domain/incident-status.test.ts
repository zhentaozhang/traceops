/**
 * The status-machine mirror must agree with the backend, or the console
 * offers actions the server will refuse.
 *
 * The expectations below are transcribed from the Java side:
 *   control-plane/.../domain/IncidentStatus.java   — the ALLOWED table
 *   control-plane/.../domain/Incident.java         — assign / acknowledge / confirm
 */
import { describe, expect, it } from "vitest";
import {
  allowedTransitions,
  canAcknowledge,
  canConfirm,
  canTransition,
  isTerminalStatus,
  parseIncidentStatus,
  STATUS_LABELS,
  TRANSITION_LABELS,
} from "./incident-status";

describe("parseIncidentStatus", () => {
  it("认得全部五个状态", () => {
    for (const s of ["NEW", "INVESTIGATING", "DIAGNOSED", "RESOLVED", "FAILED"]) {
      expect(parseIncidentStatus(s)).toBe(s);
    }
  });

  it("大小写不敏感", () => {
    expect(parseIncidentStatus("investigating")).toBe("INVESTIGATING");
    expect(parseIncidentStatus("  Diagnosed  ")).toBe("DIAGNOSED");
  });

  it("不认识的状态返回 null，而不是猜一个", () => {
    expect(parseIncidentStatus("OPEN")).toBeNull();
    expect(parseIncidentStatus("CONFIRMED")).toBeNull();
    expect(parseIncidentStatus("ACKNOWLEDGED")).toBeNull();
    expect(parseIncidentStatus("")).toBeNull();
    expect(parseIncidentStatus(null)).toBeNull();
    expect(parseIncidentStatus(42)).toBeNull();
    expect(parseIncidentStatus({})).toBeNull();
  });
});

describe("allowedTransitions", () => {
  it("与后端白名单逐条一致", () => {
    expect(allowedTransitions("NEW")).toEqual(["INVESTIGATING", "FAILED"]);
    expect(allowedTransitions("INVESTIGATING")).toEqual(["DIAGNOSED", "FAILED"]);
    expect(allowedTransitions("DIAGNOSED")).toEqual(["RESOLVED", "FAILED"]);
    expect(allowedTransitions("RESOLVED")).toEqual([]);
    expect(allowedTransitions("FAILED")).toEqual([]);
  });

  it("未知状态没有出边——不认识的状态不能被当成「什么都可以」", () => {
    expect(allowedTransitions("OPEN")).toEqual([]);
    expect(allowedTransitions(undefined)).toEqual([]);
    expect(allowedTransitions(null)).toEqual([]);
  });
});

describe("canTransition", () => {
  it("白名单内的边为真", () => {
    expect(canTransition("NEW", "INVESTIGATING")).toBe(true);
    expect(canTransition("NEW", "FAILED")).toBe(true);
    expect(canTransition("INVESTIGATING", "DIAGNOSED")).toBe(true);
    expect(canTransition("DIAGNOSED", "RESOLVED")).toBe(true);
    expect(canTransition("DIAGNOSED", "FAILED")).toBe(true);
  });

  it("白名单外的边为假", () => {
    expect(canTransition("NEW", "RESOLVED")).toBe(false);
    expect(canTransition("NEW", "DIAGNOSED")).toBe(false);
    expect(canTransition("INVESTIGATING", "RESOLVED")).toBe(false);
    expect(canTransition("DIAGNOSED", "INVESTIGATING")).toBe(false);
    expect(canTransition("RESOLVED", "FAILED")).toBe(false);
    expect(canTransition("FAILED", "RESOLVED")).toBe(false);
  });

  it("未知源状态或未知目标都是假", () => {
    expect(canTransition("OPEN", "RESOLVED")).toBe(false);
    expect(canTransition("NEW", "WHATEVER")).toBe(false);
  });
});

describe("isTerminalStatus", () => {
  it("RESOLVED 与 FAILED 是终态", () => {
    expect(isTerminalStatus("RESOLVED")).toBe(true);
    expect(isTerminalStatus("FAILED")).toBe(true);
  });

  it("其余不是终态", () => {
    expect(isTerminalStatus("NEW")).toBe(false);
    expect(isTerminalStatus("INVESTIGATING")).toBe(false);
    expect(isTerminalStatus("DIAGNOSED")).toBe(false);
    expect(isTerminalStatus("OPEN")).toBe(false);
  });
});

describe("canConfirm", () => {
  it("只有 DIAGNOSED 才能确认结论", () => {
    expect(canConfirm("DIAGNOSED", null)).toBe(true);
    expect(canConfirm("INVESTIGATING", null)).toBe(false);
    expect(canConfirm("NEW", null)).toBe(false);
    expect(canConfirm("RESOLVED", null)).toBe(false);
    expect(canConfirm("FAILED", null)).toBe(false);
  });

  it("已经确认过就不能再确认——确认不可改写", () => {
    expect(canConfirm("DIAGNOSED", "ACCEPTED")).toBe(false);
    expect(canConfirm("DIAGNOSED", "REJECTED")).toBe(false);
  });

  it("未知状态不能确认", () => {
    expect(canConfirm("OPEN", null)).toBe(false);
    expect(canConfirm(undefined, null)).toBe(false);
  });
});

describe("canAcknowledge", () => {
  it("已解决的事故没有可认领的东西", () => {
    expect(canAcknowledge("RESOLVED", null)).toBe(false);
  });

  it("没认领过就能认领", () => {
    expect(canAcknowledge("NEW", null)).toBe(true);
    expect(canAcknowledge("INVESTIGATING", null)).toBe(true);
    expect(canAcknowledge("DIAGNOSED", null)).toBe(true);
    expect(canAcknowledge("FAILED", null)).toBe(true);
  });

  it("认领过就不用再认领（首次时间不能被覆盖）", () => {
    expect(canAcknowledge("INVESTIGATING", "2026-03-12T14:33:02Z")).toBe(false);
  });
});

describe("标签表", () => {
  it("每个状态都有中文标签和流转标签", () => {
    for (const s of ["NEW", "INVESTIGATING", "DIAGNOSED", "RESOLVED", "FAILED"] as const) {
      expect(STATUS_LABELS[s]).toBeTruthy();
      expect(TRANSITION_LABELS[s]).toBeTruthy();
    }
  });
});
