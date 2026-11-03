/**
 * 人工标注的测试。
 *
 * 标注存在 localStorage 里——这是**临时**方案（控制面还没有写接口），
 * 所以行为必须满足两条：①浏览器数据被写坏时页面不能崩 ②标注永远只属于
 * 一次 run，不会串到别的 run 上。
 */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useAnnotations } from "./annotations";

const KEY = "traceops.annotations.v1";

beforeEach(() => {
  window.localStorage.clear();
});

describe("useAnnotations", () => {
  it("初始是空的", () => {
    const { result } = renderHook(() => useAnnotations("run-1"));
    expect(result.current.items).toEqual([]);
  });

  it("添加后能读回来", () => {
    const { result } = renderHook(() => useAnnotations("run-1"));
    act(() => {
      result.current.add({
        sourceId: "h1",
        targetId: "e1",
        relationType: "CORRELATED_WITH",
        note: "看起来同一批故障",
      });
    });
    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0]?.sourceId).toBe("h1");
    expect(result.current.items[0]?.runId).toBe("run-1");
  });

  it("标注按 run 隔离，不会串", () => {
    const a = renderHook(() => useAnnotations("run-1"));
    act(() => {
      a.result.current.add({
        sourceId: "h1",
        targetId: "e1",
        relationType: "RELATED",
        note: null,
      });
    });

    const b = renderHook(() => useAnnotations("run-2"));
    expect(b.result.current.items).toEqual([]);

    act(() => {
      b.result.current.add({
        sourceId: "h2",
        targetId: "e2",
        relationType: "RELATED",
        note: null,
      });
    });

    expect(a.result.current.items).toHaveLength(1);
    expect(b.result.current.items).toHaveLength(1);
    expect(a.result.current.items[0]?.sourceId).toBe("h1");
    expect(b.result.current.items[0]?.sourceId).toBe("h2");
  });

  it("删除指定标注", () => {
    const { result } = renderHook(() => useAnnotations("run-1"));
    act(() => {
      result.current.add({ sourceId: "h1", targetId: "e1", relationType: "R", note: null });
      result.current.add({ sourceId: "h2", targetId: "e2", relationType: "R", note: null });
    });
    const target = result.current.items[0]?.id ?? "";
    act(() => result.current.remove(target));
    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0]?.sourceId).toBe("h2");
  });

  it("runId 为 null 时什么都不做", () => {
    const { result } = renderHook(() => useAnnotations(null));
    act(() => {
      result.current.add({ sourceId: "h", targetId: "e", relationType: "R", note: null });
    });
    expect(result.current.items).toEqual([]);
  });

  it("localStorage 里是坏 JSON 时退化成空，不抛异常", () => {
    window.localStorage.setItem(KEY, "{not valid json");
    const { result } = renderHook(() => useAnnotations("run-1"));
    expect(result.current.items).toEqual([]);
  });

  it("localStorage 里是数组时也退化成空", () => {
    window.localStorage.setItem(KEY, "[1,2,3]");
    const { result } = renderHook(() => useAnnotations("run-1"));
    expect(result.current.items).toEqual([]);
  });

  it("写入的存储结构是 runId → 标注数组", () => {
    const { result } = renderHook(() => useAnnotations("run-1"));
    act(() => {
      result.current.add({ sourceId: "h1", targetId: "e1", relationType: "R", note: "n" });
    });
    const stored = JSON.parse(window.localStorage.getItem(KEY) ?? "{}");
    expect(Array.isArray(stored["run-1"])).toBe(true);
    expect(stored["run-1"]).toHaveLength(1);
  });
});
