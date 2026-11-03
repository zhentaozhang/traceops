/**
 * Data access seam.
 *
 * The console reads through this interface, never through `fetch` directly.
 * Today it is backed by fixtures derived from real repository output; when the
 * control plane is ready, one implementation swaps and nothing above it moves.
 */

import { toHealthVM, toIncidentVM, toReportVM, toRunVM } from "../domain/adapters";
import type { HealthVM, IncidentVM, ReportVM, RunVM } from "../domain/types";
import evaluationAblation from "../fixtures/evaluation-ablation.json";
import evaluationLatest from "../fixtures/evaluation-latest.json";
import healthFixture from "../fixtures/health.json";
import incidentsFixture from "../fixtures/incidents.json";
import progressFixture from "../fixtures/progress.json";
import abstainedReport from "../fixtures/report-abstained.json";
import inconclusiveReport from "../fixtures/report-inconclusive.json";
import refutedReport from "../fixtures/report-refuted.json";
import supportedReport from "../fixtures/report-supported.json";
import topologyFixture from "../fixtures/topology.json";

export interface IncidentFilter {
  service?: string;
  lane?: string;
  severity?: string;
}

export interface EvidenceDetailVM {
  id: string;
  evidenceType: string;
  serviceId: string | null;
  observedAt: string | null;
  windowStart: string | null;
  windowEnd: string | null;
  queryTemplateId: string | null;
  renderedQuery: string | null;
  sourceLink: string | null;
  checksum: string | null;
  normalizedData: unknown;
  rawPayload: unknown;
}

export interface EvaluationVM {
  generatedAt: string | null;
  mode: string | null;
  note: string | null;
  aggregate: Record<string, number>;
  funnel: Record<string, number>;
  chanceBaseline: Record<string, number | null>;
  naiveBaselines: Record<string, unknown>;
  scenarios: Array<Record<string, unknown>>;
  arms: Record<string, { digest: string }>;
  armResults: Record<string, Array<Record<string, unknown>>>;
  runnableArms: string[];
}

/**
 * An edge between two pieces of evidence.
 *
 * Today these come from the report's `hypothesis_evidence` links (rule-created).
 * Human annotations are stored client-side until the control plane exposes a
 * write API — see `src/data/annotations.ts`.
 */
export interface EvidenceRelationVM {
  id: string;
  sourceId: string;
  targetId: string;
  relationType: string;
  reason: string | null;
  createdBy: string;
  createdAt: string | null;
  hypothesisId: string | null;
}

/**
 * A write against an incident.
 *
 * These mirror the control plane's ownership endpoints. The console used to
 * render the three action buttons with no handler at all — the API existed,
 * the UI just lied. Keeping the operations on the `DataSource` seam means
 * the HTTP implementation can drop in without the page changing.
 */
export interface IncidentAction {
  /** Who is taking it. The backend rejects blank names. */
  assignee?: string;
  /** Who pressed the button. Recorded so the decision is traceable later. */
  by?: string;
  /** Accept (true) or reject (false) the diagnosis. */
  accepted?: boolean;
  /** Target status for a transition. */
  target?: string;
}

export interface DataSource {
  listIncidents(filter?: IncidentFilter): Promise<IncidentVM[]>;
  getIncident(id: string): Promise<IncidentVM | null>;
  listServices(): Promise<string[]>;
  getRun(runId: string): Promise<RunVM | null>;
  getReport(runId: string): Promise<ReportVM | null>;
  getEvidence(runId: string): Promise<EvidenceDetailVM[]>;
  getEvidenceDetail(id: string): Promise<EvidenceDetailVM | null>;
  getRelations(runId: string): Promise<EvidenceRelationVM[]>;
  getHealth(): Promise<HealthVM>;
  getEvaluation(): Promise<EvaluationVM>;
  getTopology(): Promise<{ edges: Array<[string, string]> }>;

  /** `POST /incidents/{id}/assign` — hand the incident to someone. */
  assignIncident(id: string, assignee: string): Promise<IncidentVM>;
  /** `POST /incidents/{id}/ack` — "I'm on it". */
  acknowledgeIncident(id: string): Promise<IncidentVM>;
  /** `POST /incidents/{id}/confirm` — accept or reject the diagnosis. */
  confirmIncident(id: string, by: string, accepted: boolean): Promise<IncidentVM>;
  /** `POST /incidents/{id}/transitions` — move along the status machine. */
  transitionIncident(id: string, target: string): Promise<IncidentVM>;
}

const reports: Record<string, unknown> = {
  "run-demo-1": supportedReport,
  "run-2b9e04": inconclusiveReport,
  "run-1c4a6e": abstainedReport,
  "run-e5b118": refutedReport,
};

const rawReports: Record<string, unknown> = {
  "run-demo-1": supportedReport,
  "run-2b9e04": inconclusiveReport,
  "run-1c4a6e": abstainedReport,
  "run-e5b118": refutedReport,
};

/** Evidence ids for each run, taken from the report payload. */
function evidenceForRun(runId: string): EvidenceDetailVM[] {
  const report = rawReports[runId];
  if (!report || typeof report !== "object") return [];
  const list = Array.isArray((report as { evidence?: unknown }).evidence)
    ? (report as { evidence: unknown[] }).evidence
    : [];
  return list.map((raw) => {
    const e = raw as Record<string, unknown>;
    return {
      id: String(e.id ?? "—"),
      evidenceType: String(e.evidence_type ?? "unknown"),
      serviceId: (e.service_id as string | null) ?? null,
      observedAt: (e.observed_at as string | null) ?? null,
      windowStart: (e.window_start as string | null) ?? null,
      windowEnd: (e.window_end as string | null) ?? null,
      queryTemplateId: (e.query_template_id as string | null) ?? null,
      renderedQuery: (e.rendered_query as string | null) ?? null,
      sourceLink: (e.source_link as string | null) ?? null,
      checksum: (e.checksum as string | null) ?? null,
      normalizedData: e.normalized_data ?? null,
      rawPayload: e.raw_payload ?? e.normalized_data ?? null,
    };
  });
}

const allEvidence = new Map<string, EvidenceDetailVM>();
for (const runId of Object.keys(rawReports)) {
  for (const e of evidenceForRun(runId)) allEvidence.set(e.id, e);
}

/**
 * Evidence relations.
 *
 * The report links hypotheses to evidence; we invert that into an edge list so
 * the Evidence Explorer can draw one graph instead of five separate lists.
 */
function relationsForRun(runId: string): EvidenceRelationVM[] {
  const report = rawReports[runId];
  if (!report || typeof report !== "object") return [];
  const links = (report as { hypothesis_evidence?: unknown }).hypothesis_evidence;
  if (!Array.isArray(links)) return [];
  return links.flatMap((raw, i) => {
    if (typeof raw !== "object" || raw === null) return [];
    const l = raw as Record<string, unknown>;
    const hypothesisId = typeof l.hypothesis_id === "string" ? l.hypothesis_id : null;
    const evidenceId = typeof l.evidence_id === "string" ? l.evidence_id : null;
    if (!hypothesisId || !evidenceId) return [];
    return [
      {
        id: `rel-${i}-${hypothesisId.slice(0, 8)}-${evidenceId.slice(0, 8)}`,
        sourceId: hypothesisId,
        targetId: evidenceId,
        relationType: typeof l.relation_type === "string" ? l.relation_type : "RELATED",
        reason: typeof l.reason === "string" ? l.reason : null,
        createdBy: typeof l.created_by === "string" ? l.created_by : "unknown",
        createdAt: typeof l.created_at === "string" ? l.created_at : null,
        hypothesisId,
      },
    ];
  });
}

function delay<T>(value: T, ms = 90): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

class FixtureDataSource implements DataSource {
  async listIncidents(filter: IncidentFilter = {}): Promise<IncidentVM[]> {
    let list = (incidentsFixture.incidents as unknown[]).map(toIncidentVM);
    if (filter.service) list = list.filter((i) => i.service === filter.service);
    if (filter.lane) list = list.filter((i) => i.stage?.lane === filter.lane);
    if (filter.severity) list = list.filter((i) => i.severity === filter.severity);
    return delay(list);
  }

  async getIncident(id: string): Promise<IncidentVM | null> {
    const raw = (incidentsFixture.incidents as Array<{ id: string }>).find((i) => i.id === id);
    return delay(raw ? toIncidentVM(raw) : null);
  }

  async listServices(): Promise<string[]> {
    const services = new Set(
      (incidentsFixture.incidents as Array<{ service: string }>).map((i) => i.service),
    );
    return delay([...services].sort());
  }

  async getRun(runId: string): Promise<RunVM | null> {
    const w = (progressFixture as Record<string, unknown>)[runId];
    return delay(w ? toRunVM(w) : null);
  }

  async getReport(runId: string): Promise<ReportVM | null> {
    const w = reports[runId];
    return delay(w ? toReportVM(w) : null);
  }

  async getEvidence(runId: string): Promise<EvidenceDetailVM[]> {
    return delay(evidenceForRun(runId));
  }

  async getEvidenceDetail(id: string): Promise<EvidenceDetailVM | null> {
    return delay(allEvidence.get(id) ?? null);
  }

  async getRelations(runId: string): Promise<EvidenceRelationVM[]> {
    return delay(relationsForRun(runId));
  }

  async getHealth(): Promise<HealthVM> {
    return delay(toHealthVM(healthFixture));
  }

  async getEvaluation(): Promise<EvaluationVM> {
    const latest = evaluationLatest as unknown as {
      generated_at?: string;
      mode?: string;
      note?: string;
      aggregate?: Record<string, number>;
      funnel?: Record<string, number>;
      chance_baseline?: Record<string, number | null>;
      naive_baselines?: Record<string, unknown>;
      scenarios?: Array<Record<string, unknown>>;
    };
    const ablation = evaluationAblation as unknown as {
      arms?: Record<string, { digest: string }>;
      results?: Record<string, Array<Record<string, unknown>>>;
      runnable_arms?: string[];
    };
    return delay({
      generatedAt: latest.generated_at ?? null,
      mode: latest.mode ?? null,
      note: latest.note ?? null,
      aggregate: latest.aggregate ?? {},
      funnel: latest.funnel ?? {},
      chanceBaseline: latest.chance_baseline ?? {},
      naiveBaselines: latest.naive_baselines ?? {},
      scenarios: latest.scenarios ?? [],
      arms: ablation.arms ?? {},
      armResults: ablation.results ?? {},
      runnableArms: ablation.runnable_arms ?? [],
    });
  }

  async getTopology(): Promise<{ edges: Array<[string, string]> }> {
    return delay(topologyFixture as { edges: Array<[string, string]> });
  }

  /* ── writes ──────────────────────────────────────────────────────────────
   *
   * The fixture simulates the control plane's rules, not just its endpoints.
   * A fixture that accepts everything would let the UI ship a button that
   * looks right and then fails on the first real deployment, which is the
   * exact failure mode the three dead buttons already had.
   */

  async assignIncident(id: string, assignee: string): Promise<IncidentVM> {
    const name = assignee.trim();
    if (!name) throw new Error("负责人不能为空");
    const incident = requireIncident(id);
    return delay({
      ...incident,
      assignee: name,
      updatedAt: new Date().toISOString(),
    });
  }

  async acknowledgeIncident(id: string): Promise<IncidentVM> {
    const incident = requireIncident(id);
    if (incident.status === "RESOLVED") {
      throw new Error("事故已解决，没有可认领");
    }
    return delay({
      ...incident,
      // Repeat acknowledgements keep the first timestamp — matching the
      // backend, so "how long was it unattended" stays truthful.
      acknowledgedAt: incident.acknowledgedAt ?? new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });
  }

  async confirmIncident(id: string, by: string, accepted: boolean): Promise<IncidentVM> {
    const name = by.trim();
    if (!name) throw new Error("确认需要署名");
    const incident = requireIncident(id);
    if (incident.status !== "DIAGNOSED") {
      throw new Error(`事故当前是 ${incident.status}，只有 DIAGNOSED 才有结论可确认`);
    }
    if (incident.confirmation) {
      throw new Error(
        `事故已由 ${incident.confirmedBy} ${incident.confirmation === "ACCEPTED" ? "确认" : "拒绝"}，结论不能改写`,
      );
    }
    return delay({
      ...incident,
      confirmation: accepted ? "ACCEPTED" : "REJECTED",
      confirmedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });
  }

  async transitionIncident(id: string, target: string): Promise<IncidentVM> {
    const incident = requireIncident(id);
    const allowed: Record<string, string[]> = {
      NEW: ["INVESTIGATING", "FAILED"],
      INVESTIGATING: ["DIAGNOSED", "FAILED"],
      DIAGNOSED: ["RESOLVED", "FAILED"],
      RESOLVED: [],
      FAILED: [],
    };
    const from = incident.status;
    const reachable = allowed[from] ?? [];
    if (!reachable.includes(target)) {
      throw new Error(
        reachable.length === 0
          ? `事故已处于终态 ${from}，无法流转`
          : `${from} 只能流转到 ${reachable.join(" / ")}`,
      );
    }
    return delay({ ...incident, status: target, updatedAt: new Date().toISOString() });
  }
}

/**
 * Look up an incident in the fixture.
 *
 * Throws rather than returning null: a write against a missing incident is a
 * programming error, not an empty state.
 */
function requireIncident(id: string): IncidentVM {
  const raw = (incidentsFixture.incidents as Array<{ id: string }>).find((i) => i.id === id);
  if (!raw) throw new Error(`事故不存在：${id}`);
  return toIncidentVM(raw);
}

export const dataSource: DataSource = new FixtureDataSource();
