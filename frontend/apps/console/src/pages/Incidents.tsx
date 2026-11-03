/**
 * Incident list — the filterable board.
 */
import { useMemo, useState } from "react";
import { ArrowRight } from "../app/icons";
import { Link } from "../app/link";
import { urgencyOf } from "../domain/adapters";
import { formatInstant, formatRelative, LANE_LABELS, STAGE_LABELS } from "../domain/format";
import type { IncidentVM } from "../domain/types";
import { EmptyState, Ident, Panel, SeverityBadge, StageBadge } from "../ui/primitives";

type SortKey = "urgency" | "created" | "severity";

const SEVERITY_ORDER: Record<string, number> = {
  CRITICAL: 0,
  MAJOR: 1,
  MINOR: 2,
  INFO: 3,
  UNKNOWN: 4,
};

export function IncidentsPage({ incidents }: { incidents: IncidentVM[] }) {
  const [service, setService] = useState<string>("");
  const [lane, setLane] = useState<string>("");
  const [sort, setSort] = useState<SortKey>("urgency");

  const services = useMemo(
    () => [...new Set(incidents.map((i) => i.service))].sort(),
    [incidents],
  );

  const lanes = useMemo(() => {
    const set = new Set(incidents.map((i) => i.stage?.lane).filter(Boolean));
    return [...set] as string[];
  }, [incidents]);

  const visible = useMemo(() => {
    let list = incidents.filter(
      (i) => (!service || i.service === service) && (!lane || i.stage?.lane === lane),
    );
    list = [...list].sort((a, b) => {
      if (sort === "created") return b.createdAt.localeCompare(a.createdAt);
      if (sort === "severity") {
        return (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9);
      }
      return urgencyOf(a.stage) - urgencyOf(b.stage) || a.createdAt.localeCompare(b.createdAt);
    });
    return list;
  }, [incidents, service, lane, sort]);

  const laneCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const i of incidents) {
      const l = i.stage?.lane ?? "";
      if (l) m.set(l, (m.get(l) ?? 0) + 1);
    }
    return m;
  }, [incidents]);

  return (
    <>
      <div className="page-head">
        <div className="page-head__text">
          <div className="eyebrow">
            <span className="label">事故</span>
          </div>
          <h1 className="page-title">事故列表</h1>
          <p className="page-sub">
            默认按等待对象排序——等人决策的排最前。切换到「按创建时间」可以看最近发生了什么。
          </p>
        </div>
      </div>

      <div className="filters">
        <select
          className="select"
          name="service"
          value={service}
          onChange={(e) => setService(e.target.value)}
          aria-label="按服务过滤"
        >
          <option value="">全部服务</option>
          {services.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        <button
          type="button"
          className={`chip${lane === "" ? " is-on" : ""}`}
          onClick={() => setLane("")}
        >
          全部
          <span className="chip__count">{incidents.length}</span>
        </button>
        {lanes.map((l) => (
          <button
            key={l}
            type="button"
            className={`chip${lane === l ? " is-on" : ""}`}
            onClick={() => setLane(lane === l ? "" : l)}
          >
            {LANE_LABELS[l] ?? l}
            <span className="chip__count">{laneCounts.get(l) ?? 0}</span>
          </button>
        ))}

        <div className="spacer" />

        <select
          className="select"
          name="sort"
          value={sort}
          onChange={(e) => setSort(e.target.value as SortKey)}
          aria-label="排序方式"
        >
          <option value="urgency">按等待对象</option>
          <option value="created">按创建时间</option>
          <option value="severity">按严重度</option>
        </select>
      </div>

      {visible.length === 0 ? (
        <Panel>
          <EmptyState title="没有匹配的事故" detail="调整过滤条件，或清空服务与状态筛选。" />
        </Panel>
      ) : (
        <Panel className="panel--flush">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 96 }}>严重度</th>
                  <th style={{ width: 176 }}>服务</th>
                  <th>标题</th>
                  <th style={{ width: 132 }}>状态</th>
                  <th style={{ width: 108 }}>负责</th>
                  <th style={{ width: 74 }}>重复</th>
                  <th style={{ width: 116 }}>更新</th>
                  <th style={{ width: 32 }} />
                </tr>
              </thead>
              <tbody>
                {visible.map((inc) => (
                  <tr key={inc.id} className="is-clickable">
                    <td>
                      <SeverityBadge severity={inc.severity} />
                    </td>
                    <td className="cell-primary mono">{inc.service}</td>
                    <td className="clip" style={{ maxWidth: 460 }}>
                      {/* The link's hit area is stretched over the whole row
                          (see .row-link::after), so the mouse still gets one
                          big target — but the keyboard gets a real <a>. */}
                      <Link to={`/incidents/${inc.id}`} className="row-link">
                        {inc.title}
                      </Link>
                    </td>
                    <td>
                      <StageBadge
                        kind={inc.stage?.kind ?? "UNKNOWN"}
                        label={STAGE_LABELS[inc.stage?.kind ?? "UNKNOWN"] ?? "未知"}
                        running={inc.stage?.kind === "RUNNING"}
                      />
                    </td>
                    <td>
                      {inc.assignee ? (
                        <Ident className="mono dim">{inc.assignee}</Ident>
                      ) : (
                        <span className="faint">未指派</span>
                      )}
                    </td>
                    <td className="cell-num faint">
                      {inc.duplicateAlerts ? `×${inc.duplicateAlerts}` : "—"}
                    </td>
                    <td className="faint" title={formatInstant(inc.updatedAt)}>
                      {formatRelative(inc.updatedAt)}
                    </td>
                    <td className="faint">
                      <ArrowRight size={13} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </>
  );
}
