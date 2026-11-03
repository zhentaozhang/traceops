/**
 * Local annotations.
 *
 * The control plane has no write API for human judgements yet, so operator
 * notes live in the browser. They are intentionally *not* presented as
 * authoritative: everything the UI shows from this module is labelled as a
 * local annotation until a backend endpoint exists.
 *
 * Storage is `localStorage`, keyed by run. Clearing the browser clears the
 * annotations; nothing here is a system of record.
 */
import { useCallback, useEffect, useState } from "react";

const KEY = "traceops.annotations.v1";

export interface Annotation {
  id: string;
  runId: string;
  sourceId: string;
  targetId: string;
  relationType: string;
  note: string | null;
  createdAt: string;
}

type Store = Record<string, Annotation[]>;

function readStore(): Store {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return {};
    return parsed as Store;
  } catch {
    return {};
  }
}

function writeStore(store: Store) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(store));
  } catch {
    // Storage may be unavailable (private mode, quota). Annotations are a
    // convenience; losing them must never break the page.
  }
}

export function useAnnotations(runId: string | null) {
  const [items, setItems] = useState<Annotation[]>([]);

  useEffect(() => {
    if (!runId) {
      setItems([]);
      return;
    }
    setItems(readStore()[runId] ?? []);
  }, [runId]);

  const add = useCallback(
    (input: Omit<Annotation, "id" | "createdAt" | "runId">) => {
      if (!runId) return;
      const store = readStore();
      const list = store[runId] ?? [];
      const annotation: Annotation = {
        ...input,
        id: `ann-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
        runId,
        createdAt: new Date().toISOString(),
      };
      store[runId] = [...list, annotation];
      writeStore(store);
      setItems(store[runId] ?? []);
    },
    [runId],
  );

  const remove = useCallback(
    (id: string) => {
      if (!runId) return;
      const store = readStore();
      store[runId] = (store[runId] ?? []).filter((a) => a.id !== id);
      writeStore(store);
      setItems(store[runId] ?? []);
    },
    [runId],
  );

  return { items, add, remove };
}
