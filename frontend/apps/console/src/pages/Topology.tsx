/**
 * Service Topology.
 *
 * The declared graph is a fact about configuration; the observed one is a fact
 * about traffic. The interesting part is where they disagree — so the view
 * shows both and lets you pick the service you care about.
 *
 * Layout is a deterministic layered placement (no physics simulation): the
 * same graph must draw the same way every time, or operators lose the map.
 */
import { useEffect, useMemo, useState } from "react";
import { navigate } from "../app/router";
import { dataSource } from "../data/source";
import { formatCount } from "../domain/format";
import type { IncidentVM } from "../domain/types";
import { Badge, EmptyState, Panel, Stat } from "../ui/primitives";

interface Node {
  id: string;
  layer: number;
  x: number;
  y: number;
}

export function TopologyPage() {
  const [edges, setEdges] = useState<Array<[string, string]>>([]);
  const [incidents, setIncidents] = useState<IncidentVM[]>([]);
  const [focus, setFocus] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    void Promise.all([dataSource.getTopology(), dataSource.listIncidents()])
      .then(([topo, incs]) => {
        if (!alive) return;
        setEdges(topo.edges);
        setIncidents(incs);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);

  const graph = useMemo(() => layout(edges), [edges]);

  /**
   * The topology graph and the incident list do not agree on service naming —
   * one comes from the online-boutique topology file (`checkoutservice`), the
   * other from the alert payload (`checkout-service`). Joining them on a
   * normalised key keeps the incident links alive without editing either
   * source of truth.
   */
  const incidentsByService = useMemo(() => {
    const map = new Map<string, IncidentVM[]>();
    for (const inc of incidents) {
      const key = serviceKey(inc.service);
      const list = map.get(key) ?? [];
      list.push(inc);
      map.set(key, list);
    }
    return map;
  }, [incidents]);

  const nodeMap = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph.nodes]);

  /** Edges touching the focused service, plus its direct neighbours. */
  const visible = useMemo(() => {
    if (!focus) return graph;
    const keep = new Set<string>([focus]);
    for (const [from, to] of graph.edges) {
      if (from === focus) keep.add(to);
      if (to === focus) keep.add(from);
    }
    return {
      nodes: graph.nodes.filter((n) => keep.has(n.id)),
      edges: graph.edges.filter(([f, t]) => keep.has(f) && keep.has(t)),
    };
  }, [focus, graph]);

  const services = useMemo(
    () => [...new Set(graph.nodes.map((n) => n.id))].sort(),
    [graph.nodes],
  );

  return (
    <>
      <div className="page-head">
        <div className="page-head__text">
          <div className="eyebrow">
            <span className="label">服务拓扑</span>
          </div>
          <h1 className="page-title">调用关系图</h1>
          <p className="page-sub">
            声明的依赖来自配置，实测的依赖来自流量。两者不一致的地方通常就是故障的形状——
            先看图，再看某个服务身上的事故。
          </p>
        </div>
        <div className="page-actions">
          {focus && (
            <button type="button" className="chip is-on" onClick={() => setFocus(null)}>
              聚焦 {focus} · 取消
            </button>
          )}
        </div>
      </div>

      <div className="stat-row">
        <Stat label="服务" value={graph.nodes.length} />
        <Stat label="依赖边" value={graph.edges.length} />
        <Stat
          label="有事故"
          value={incidentsByService.size}
          hint="至少有一笔未收口事故的服务"
          tone={incidentsByService.size > 0 ? "accent" : "neutral"}
        />
      </div>

      <div className="section">
        <div className="section__head">
          <span className="label">图</span>
          <div className="section__rule" />
          {focus && <span className="section__meta">只看 {focus} 的邻居</span>}
        </div>

        {failed ? (
          <Panel>
            <EmptyState title="取不到拓扑" detail="拓扑接口不可用。" />
          </Panel>
        ) : graph.nodes.length === 0 ? (
          <Panel>
            <EmptyState title="没有拓扑数据" detail="接入控制面后这里会显示声明的依赖图。" />
          </Panel>
        ) : (
          <div className="topo" role="img" aria-label="服务依赖图">
            {/* biome-ignore lint/a11y/noSvgWithoutTitle: the wrapper carries the accessible name; the SVG is presentational */}
            <svg className="topo__svg" viewBox="0 0 1000 520" preserveAspectRatio="none">
              {visible.edges.map(([from, to]) => {
                const a = nodeMap.get(from);
                const b = nodeMap.get(to);
                if (!a || !b) return null;
                const dim = focus !== null && from !== focus && to !== focus;
                return (
                  <line
                    key={`${from}->${to}`}
                    x1={(a.x / 1000) * 1000}
                    y1={(a.y / 520) * 520}
                    x2={(b.x / 1000) * 1000}
                    y2={(b.y / 520) * 520}
                    stroke="var(--border-strong)"
                    strokeWidth="1"
                    opacity={dim ? 0.18 : 0.65}
                  />
                );
              })}
            </svg>
            {visible.nodes.map((n) => {
              const incs = incidentsByService.get(serviceKey(n.id)) ?? [];
              const hot = incs.some((i) => i.severity === "CRITICAL");
              const busy = incs.length > 0;
              return (
                <button
                  key={n.id}
                  type="button"
                  className={`topo__node${hot ? " topo__node--hot" : ""}`}
                  style={{ left: `${(n.x / 1000) * 100}%`, top: `${(n.y / 520) * 100}%` }}
                  onClick={() => setFocus(focus === n.id ? null : n.id)}
                  title={busy ? `${n.id} · ${incs.length} 起事故` : n.id}
                >
                  {n.id}
                  {busy && (
                    <span
                      style={{
                        marginLeft: 6,
                        color: hot ? "inherit" : "var(--severity-major)",
                      }}
                    >
                      ●{incs.length}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="split section">
        <Panel title="服务" meta={`${services.length} 个`} className="panel--flush">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>服务</th>
                  <th style={{ width: 80 }}>出边</th>
                  <th style={{ width: 80 }}>入边</th>
                  <th style={{ width: 110 }}>事故</th>
                </tr>
              </thead>
              <tbody>
                {services.map((s) => {
                  const incs = incidentsByService.get(serviceKey(s)) ?? [];
                  return (
                    <tr
                      key={s}
                      className="is-clickable"
                      onClick={() => setFocus(focus === s ? null : s)}
                    >
                      <td className="cell-primary mono">{s}</td>
                      <td className="cell-num">
                        {graph.edges.filter(([f]) => f === s).length}
                      </td>
                      <td className="cell-num">
                        {graph.edges.filter(([, t]) => t === s).length}
                      </td>
                      <td>
                        {incs.length === 0 ? (
                          <span className="faint">—</span>
                        ) : (
                          <div className="row" style={{ gap: 4 }}>
                            <Badge tone="accent">{formatCount(incs.length)}</Badge>
                            {incs[0] && (
                              <button
                                type="button"
                                className="mono"
                                style={{ color: "var(--accent-strong)" }}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  navigate(`/incidents/${incs[0]!.id}`);
                                }}
                              >
                                打开
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="依赖明细">
          {focus ? (
            <div className="stack">
              <div className="row">
                <span className="mono" style={{ color: "var(--text-primary)" }}>
                  {focus}
                </span>
                <Badge tone="accent">聚焦中</Badge>
              </div>
              <div className="notes">
                {graph.edges
                  .filter(([f, t]) => f === focus || t === focus)
                  .map(([f, t]) => (
                    <div className="note" key={`${f}->${t}`}>
                      <span className="note__bullet" />
                      <span className="mono">
                        {f} → {t}
                      </span>
                    </div>
                  ))}
              </div>
            </div>
          ) : (
            <EmptyState
              title="选中一个服务"
              detail="点击图上的节点或表格里的行，这里会列出它的直接依赖。"
            />
          )}
        </Panel>
      </div>
    </>
  );
}

/**
 * Normalise a service name so `checkout-service` and `checkoutservice` are
 * the same service. Lower-case, strip separators — nothing else.
 */
function serviceKey(name: string): string {
  return name.toLowerCase().replace(/[-_.]/g, "");
}

/**
 * Deterministic layered layout.
 *
 * Layer = longest path from any root, so a service always sits to the right of
 * everything it calls. Ties are broken by name, which keeps the drawing
 * stable across reloads.
 */
function layout(edges: Array<[string, string]>): {
  nodes: Node[];
  edges: Array<[string, string]>;
} {
  const names = new Set<string>();
  for (const [from, to] of edges) {
    names.add(from);
    names.add(to);
  }
  if (names.size === 0) return { nodes: [], edges };

  const outgoing = new Map<string, string[]>();
  const indegree = new Map<string, number>();
  for (const n of names) {
    outgoing.set(n, []);
    indegree.set(n, 0);
  }
  for (const [from, to] of edges) {
    outgoing.get(from)?.push(to);
    indegree.set(to, (indegree.get(to) ?? 0) + 1);
  }

  // Kahn's algorithm for the longest-path layering.
  const layer = new Map<string, number>();
  const queue = [...names].filter((n) => (indegree.get(n) ?? 0) === 0).sort();
  for (const n of queue) layer.set(n, 0);
  let head = 0;
  while (head < queue.length) {
    const n = queue[head++]!;
    for (const next of outgoing.get(n) ?? []) {
      layer.set(next, Math.max(layer.get(next) ?? 0, (layer.get(n) ?? 0) + 1));
      indegree.set(next, (indegree.get(next) ?? 1) - 1);
      if (indegree.get(next) === 0) queue.push(next);
    }
  }
  for (const n of names) if (!layer.has(n)) layer.set(n, 0);

  const byLayer = new Map<number, string[]>();
  for (const n of [...names].sort()) {
    const l = layer.get(n) ?? 0;
    const list = byLayer.get(l) ?? [];
    list.push(n);
    byLayer.set(l, list);
  }

  const maxLayer = Math.max(...byLayer.keys());
  const nodes: Node[] = [];
  for (const [l, list] of byLayer) {
    const x = maxLayer === 0 ? 500 : 140 + (l / maxLayer) * 720;
    list.forEach((name, i) => {
      const y = ((i + 1) / (list.length + 1)) * 520;
      nodes.push({ id: name, layer: l, x, y });
    });
  }

  return { nodes, edges };
}
