/**
 * Application root: data loading, routing, shell, command palette.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { dataSource } from "../data/source";
import type { IncidentVM } from "../domain/types";
import { EvaluationPage } from "../pages/Evaluation";
import { EvidencePage } from "../pages/Evidence";
import { IncidentPage } from "../pages/Incident";
import { IncidentsPage } from "../pages/Incidents";
import { NotFound } from "../pages/NotFound";
import { OpsPage } from "../pages/Ops";
import { OverviewPage } from "../pages/Overview";
import { TopologyPage } from "../pages/Topology";
import { useRoute } from "./router";
import { CommandPalette, Rail, Topbar, useIncidentCommands } from "./shell";

const ROUTES: Array<[string, string]> = [
  ["/", "overview"],
  ["/incidents", "incidents"],
  ["/incidents/:id", "incident"],
  ["/incidents/:id/runs/:runId", "run"],
  ["/evidence", "evidence"],
  ["/topology", "topology"],
  ["/evaluation", "evaluation"],
  ["/ops", "ops"],
];

export function App() {
  const { key, params, path, navigate } = useRoute(ROUTES);
  const [incidents, setIncidents] = useState<IncidentVM[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    void dataSource.listIncidents().then(setIncidents);
  }, []);

  const incidentCommands = useIncidentCommands(incidents, navigate);

  const commands = useMemo(
    () => [
      ...incidentCommands,
      {
        id: "nav-overview",
        label: "前往概览",
        group: "导航",
        hint: "/",
        run: () => navigate("/"),
      },
      {
        id: "nav-incidents",
        label: "前往事故列表",
        group: "导航",
        hint: "/incidents",
        run: () => navigate("/incidents"),
      },
      {
        id: "nav-evidence",
        label: "前往证据浏览器",
        group: "导航",
        hint: "/evidence",
        run: () => navigate("/evidence"),
      },
      {
        id: "nav-topology",
        label: "前往服务拓扑",
        group: "导航",
        hint: "/topology",
        run: () => navigate("/topology"),
      },
      {
        id: "nav-evaluation",
        label: "前往评测实验室",
        group: "导航",
        hint: "/evaluation",
        run: () => navigate("/evaluation"),
      },
      {
        id: "nav-ops",
        label: "前往运维面板",
        group: "导航",
        hint: "/ops",
        run: () => navigate("/ops"),
      },
    ],
    [incidentCommands, navigate],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = useCallback(
    (to: string) => {
      navigate(to);
    },
    [navigate],
  );

  const crumbs = useMemo(() => buildCrumbs(key, params, incidents), [key, params, incidents]);

  return (
    <div className="app">
      <Rail path={path} incidents={incidents} onNavigate={go} />
      <div className="main">
        <Topbar crumbs={crumbs} onOpenPalette={() => setPaletteOpen(true)} />
        <main className="page">
          <div className="page__inner">
            {key === "overview" && <OverviewPage incidents={incidents} />}
            {key === "incidents" && <IncidentsPage incidents={incidents} />}
            {key === "incident" && <IncidentPage incidentId={params.id ?? ""} />}
            {key === "run" && (
              <IncidentPage incidentId={params.id ?? ""} runId={params.runId ?? ""} focusRun />
            )}
            {key === "evidence" && <EvidencePage />}
            {key === "topology" && <TopologyPage />}
            {key === "evaluation" && <EvaluationPage />}
            {key === "ops" && <OpsPage />}
            {key === "not-found" && <NotFound path={path} />}
          </div>
        </main>
      </div>
      {paletteOpen && (
        <CommandPalette commands={commands} onClose={() => setPaletteOpen(false)} />
      )}
    </div>
  );
}

function buildCrumbs(
  key: string,
  params: Record<string, string>,
  incidents: IncidentVM[],
): Array<{ label: string; to?: string }> {
  switch (key) {
    case "overview":
      return [{ label: "概览" }];
    case "incidents":
      return [{ label: "事故" }];
    case "incident":
    case "run": {
      const inc = incidents.find((i) => i.id === params.id);
      const crumbs: Array<{ label: string; to?: string }> = [
        { label: "事故", to: "/incidents" },
      ];
      if (inc) {
        // The last crumb is where you are, not a link back to yourself.
        crumbs.push(
          key === "incident"
            ? { label: inc.service }
            : { label: inc.service, to: `/incidents/${inc.id}` },
        );
      }
      if (params.runId) crumbs.push({ label: params.runId });
      return crumbs;
    }
    case "evidence":
      return [{ label: "证据" }];
    case "topology":
      return [{ label: "拓扑" }];
    case "evaluation":
      return [{ label: "评测" }];
    case "ops":
      return [{ label: "运维" }];
    default:
      return [{ label: "未找到" }];
  }
}
