/**
 * Wire → view model adapters.
 *
 * Rules that must hold for every adapter:
 *   1. Never throw. A malformed field degrades to null / UNKNOWN.
 *   2. Unknown enum values are preserved verbatim (`raw*`) so the UI can show
 *      what the backend actually said instead of dropping it.
 *   3. Absent optional data is `null`, never a placeholder that looks real.
 */
import type {
  ComponentHealthVM,
  EvidenceRefVM,
  FunnelVM,
  HealthVM,
  HypothesisVM,
  IncidentVM,
  Lane,
  ProgressStepVM,
  ReportVM,
  RunVM,
  Severity,
  StageKind,
  StageVM,
  Verdict,
  WaitingOn,
} from "./types";

type Dict = Record<string, unknown>;

const isDict = (v: unknown): v is Dict =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const str = (v: unknown): string | null => (typeof v === "string" && v.length > 0 ? v : null);

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

const SEVERITIES: Severity[] = ["CRITICAL", "MAJOR", "MINOR", "INFO"];
const STAGE_KINDS: StageKind[] = [
  "QUEUED",
  "RUNNING",
  "RETRY_BACKOFF",
  "NEEDS_DECISION",
  "DONE",
  "DEAD",
];
const WAITING_ON: WaitingOn[] = ["WORKER", "CLOCK", "PERSON", "NOBODY"];

function severityOf(raw: unknown): { value: Severity; raw: string } {
  const s = str(raw)?.toUpperCase() ?? "";
  if ((SEVERITIES as string[]).includes(s)) return { value: s as Severity, raw: s };
  return { value: "UNKNOWN", raw: s };
}

function stageKindOf(raw: unknown): { value: StageKind; raw: string } {
  const s = str(raw)?.toUpperCase() ?? "";
  if ((STAGE_KINDS as string[]).includes(s)) return { value: s as StageKind, raw: s };
  return { value: "UNKNOWN", raw: s };
}

function waitingOnOf(raw: unknown): { value: WaitingOn; raw: string } {
  const s = str(raw)?.toUpperCase() ?? "";
  if ((WAITING_ON as string[]).includes(s)) return { value: s as WaitingOn, raw: s };
  return { value: "UNKNOWN", raw: s };
}

function verdictOf(raw: unknown): { value: Verdict; raw: string } {
  const s = str(raw)?.toUpperCase() ?? "";
  if (s === "SUPPORTED" || s === "REFUTED" || s === "INCONCLUSIVE") {
    return { value: s, raw: s };
  }
  return { value: "UNKNOWN", raw: s };
}

/**
 * A stage's *lane* is what the operator actually triages on: is this waiting
 * on a human, on the clock, or actively running? Two incidents with the same
 * status can sit in different lanes.
 */
export function laneOf(kind: StageKind, waitingOn: WaitingOn): Lane {
  if (kind === "DONE") return "finished";
  if (kind === "DEAD") return "failed";
  if (waitingOn === "PERSON" || kind === "NEEDS_DECISION") return "waiting-person";
  if (waitingOn === "CLOCK" || kind === "RETRY_BACKOFF") return "waiting-clock";
  if (kind === "QUEUED") return "waiting-clock";
  return "active";
}

/** Sort weight for the incident list — highest urgency first. */
export function urgencyOf(stage: StageVM | null): number {
  if (!stage) return 50;
  switch (stage.lane) {
    case "waiting-person":
      return 0;
    case "failed":
      return 1;
    case "active":
      return 2;
    case "waiting-clock":
      return 3;
    default:
      return 4;
  }
}

export function toIncidentVM(wire: unknown): IncidentVM {
  const w = isDict(wire) ? wire : {};
  const stageWire = isDict(w.stage) ? w.stage : null;
  let stage: StageVM | null = null;

  if (stageWire) {
    const k = stageKindOf(stageWire.kind);
    const o = waitingOnOf(stageWire.waitingOn);
    stage = {
      kind: k.value,
      rawKind: k.raw,
      waitingOn: o.value,
      rawWaitingOn: o.raw,
      detail: str(stageWire.detail),
      retryable: stageWire.retryable === true,
      lane: laneOf(k.value, o.value),
    };
  }

  const windowWire = isDict(w.window) ? w.window : null;
  const sev = severityOf(w.severity);

  return {
    id: str(w.id) ?? "—",
    service: str(w.service) ?? "—",
    title: str(w.title) ?? "（无标题）",
    severity: sev.value,
    rawSeverity: sev.raw,
    status: str(w.status) ?? "UNKNOWN",
    origin: str(w.origin) ?? "UNKNOWN",
    createdAt: str(w.createdAt) ?? "",
    updatedAt: str(w.updatedAt),
    stage,
    assignee: str(w.assignee),
    acknowledgedAt: str(w.acknowledgedAt),
    confirmedAt: str(w.confirmedAt),
    confirmation:
      w.confirmation === "ACCEPTED" || w.confirmation === "REJECTED" ? w.confirmation : null,
    currentRunId: str(w.currentRunId),
    retryAttempts: num(w.retryAttempts),
    nextRetryAt: str(w.nextRetryAt),
    windowStart: str(windowWire?.start) ?? str(w.windowStart),
    windowEnd: str(windowWire?.end) ?? str(w.windowEnd),
    duplicateAlerts: num(w.duplicateAlerts),
  };
}

function toHypothesisVM(
  wire: unknown,
  relations: Record<string, { s: number; r: number }>,
): HypothesisVM {
  const w = isDict(wire) ? wire : {};
  const id = str(w.id) ?? "";
  const rel = relations[id] ?? { s: 0, r: 0 };
  return {
    id,
    rank: num(w.rank),
    status: str(w.status) ?? "unknown",
    service: str(w.candidate_service) ?? str(w.candidateService),
    component: str(w.candidate_component) ?? str(w.candidateComponent),
    faultType: str(w.fault_type) ?? str(w.faultType),
    mechanism: str(w.failure_mechanism) ?? str(w.mechanism),
    predictedObservations: arr(w.predicted_observations ?? w.predictedObservations).filter(
      (v): v is string => typeof v === "string",
    ),
    supportCount: rel.s,
    refuteCount: rel.r,
  };
}

export function toEvidenceRefVM(wire: unknown): EvidenceRefVM {
  const w = isDict(wire) ? wire : {};
  return {
    id: str(w.id) ?? "—",
    evidenceType: str(w.evidence_type) ?? str(w.evidenceType) ?? "unknown",
    serviceId: str(w.service_id) ?? str(w.serviceId),
    observedAt: str(w.observed_at) ?? str(w.observedAt),
    windowStart: str(w.window_start) ?? str(w.windowStart),
    windowEnd: str(w.window_end) ?? str(w.windowEnd),
    queryTemplateId: str(w.query_template_id) ?? str(w.queryTemplateId),
    renderedQuery: str(w.rendered_query) ?? str(w.renderedQuery),
    sourceLink: str(w.source_link) ?? str(w.sourceLink),
    checksum: str(w.checksum),
    traceId: str(w.trace_id) ?? str(w.traceId),
    spanId: str(w.span_id) ?? str(w.spanId),
  };
}

export function toReportVM(wire: unknown): ReportVM {
  const w = isDict(wire) ? wire : {};

  // Count supports/refutes per hypothesis so a candidate's strength is visible.
  const relations: Record<string, { s: number; r: number }> = {};
  for (const link of arr(w.hypothesis_evidence)) {
    if (!isDict(link)) continue;
    const hid = str(link.hypothesis_id) ?? str(link.hypothesisId);
    const rel = str(link.relation_type) ?? str(link.relationType) ?? "";
    if (!hid) continue;
    const slot = relations[hid] ?? { s: 0, r: 0 };
    relations[hid] = slot;
    if (rel.toUpperCase().startsWith("SUPPORT")) slot.s += 1;
    else if (rel.toUpperCase().startsWith("REFUTE")) slot.r += 1;
  }

  const v = verdictOf(w.diagnosis_status ?? w.diagnosisStatus);
  const abstained = w.abstained === true;

  const chainRaw = arr(w.propagation_chain ?? w.propagationChain);
  const propagationChain: Array<[string, string]> = [];
  for (const edge of chainRaw) {
    if (Array.isArray(edge) && edge.length >= 2) {
      const a = str(edge[0]);
      const b = str(edge[1]);
      if (a && b) propagationChain.push([a, b]);
    } else if (isDict(edge)) {
      const a = str(edge.from) ?? str(edge.source);
      const b = str(edge.to) ?? str(edge.target);
      if (a && b) propagationChain.push([a, b]);
    }
  }

  const drift = isDict(w.topology_drift) ? w.topology_drift : null;
  const cost = isDict(w.cost) ? w.cost : null;

  return {
    runId: str(w.investigation_run_id) ?? str(w.runId) ?? "—",
    verdict: abstained && v.value === "UNKNOWN" ? "UNKNOWN" : v.value,
    rawVerdict: v.raw,
    summary: str(w.summary) ?? "",
    abstained,
    hypotheses: arr(w.hypotheses).map((h) => toHypothesisVM(h, relations)),
    evidence: arr(w.evidence).map(toEvidenceRefVM),
    affectedServices: arr(w.affected_services ?? w.affectedServices).filter(
      (x): x is string => typeof x === "string",
    ),
    missingData: arr(w.missing_data ?? w.missingData).filter(
      (x): x is string => typeof x === "string",
    ),
    noData: arr(w.no_data ?? w.noData).filter((x): x is string => typeof x === "string"),
    rootCauseTime: str(w.root_cause_time) ?? str(w.rootCauseTime),
    propagationChain,
    cost: {
      toolCalls: num(cost?.toolCalls) ?? num(w.tool_calls) ?? num(w.toolCalls),
      tokens: num(cost?.tokens) ?? num(w.tokens_used) ?? num(w.tokensUsed),
    },
    generatedAt: str(w.generated_at) ?? str(w.generatedAt),
    profileDigest: str(w.profile_digest) ?? str(w.profileDigest),
    strategyDigest: str(w.strategy_digest) ?? str(w.strategyDigest),
    rejectedProposals: arr(w.rejected_proposals ?? w.rejectedProposals).flatMap((p) => {
      if (!isDict(p)) return [];
      const proposal = str(p.proposal) ?? str(p.candidate);
      const reasonCode = str(p.reason_code) ?? str(p.reasonCode);
      if (!proposal && !reasonCode) return [];
      return [
        {
          reasonCode: reasonCode ?? "UNKNOWN",
          proposal: proposal ?? "—",
          detail: str(p.detail),
        },
      ];
    }),
    shadowDecisions: arr(w.shadow_decisions ?? w.shadowDecisions).flatMap((s) => {
      if (!isDict(s)) return [];
      const candidate = str(s.candidate) ?? str(s.proposal);
      if (!candidate) return [];
      return [{ candidate, note: str(s.note) ?? str(s.detail) }];
    }),
    topologyDrift: drift
      ? {
          declared: arr(drift.declared_edges).length,
          observed: arr(drift.observed_edges).length,
          undeclared: arr(drift.undeclared_edges).length,
        }
      : null,
  };
}

function toStepVM(wire: unknown): ProgressStepVM {
  const w = isDict(wire) ? wire : {};
  return {
    stepIndex: num(w.stepIndex) ?? num(w.step_index) ?? 0,
    node: str(w.node) ?? str(w.currentNode) ?? "—",
    description: str(w.description) ?? str(w.stepDescription) ?? "",
    evidenceCount: num(w.evidenceCount) ?? num(w.evidence_count) ?? 0,
    at: str(w.at) ?? str(w.timestamp),
  };
}

export function toRunVM(wire: unknown): RunVM {
  const w = isDict(wire) ? wire : {};
  const steps = arr(w.steps).map(toStepVM);
  const current: ProgressStepVM | null =
    steps.length > 0 ? (steps[steps.length - 1] ?? null) : null;
  return {
    runId: str(w.runId) ?? str(w.run_id) ?? "—",
    startedAt: str(w.startedAt) ?? str(w.started_at),
    finishedAt: str(w.finishedAt) ?? str(w.finished_at),
    steps,
    current,
  };
}

export function toHealthVM(wire: unknown): HealthVM {
  const w = isDict(wire) ? wire : {};
  const components: ComponentHealthVM[] = arr(w.components).flatMap((c) => {
    if (!isDict(c)) return [];
    const raw = (str(c.status) ?? "UNKNOWN").toUpperCase();
    const status: ComponentHealthVM["status"] =
      raw === "UP" || raw === "DEGRADED" || raw === "DOWN" ? raw : "UNKNOWN";
    return [
      {
        name: str(c.name) ?? "—",
        status,
        detail: str(c.detail),
        claimedButSilent: c.claimedButSilent === true,
      },
    ];
  });
  const f = isDict(w.funnel) ? w.funnel : null;
  const funnel: FunnelVM | null = f
    ? {
        since: str(f.since) ?? "",
        checkedAt: str(f.checkedAt) ?? str(f.checked_at),
        alertsSeen: num(f.alertsSeen) ?? num(f.alerts_seen) ?? 0,
        alertIncidents: num(f.alertIncidents) ?? num(f.alert_incidents) ?? 0,
        manualIncidents: num(f.manualIncidents) ?? num(f.manual_incidents) ?? 0,
        foldedDuplicates: num(f.foldedDuplicates) ?? num(f.folded_duplicates) ?? 0,
        runsRequested: num(f.runsRequested) ?? num(f.runs_requested) ?? 0,
        runsCompleted: num(f.runsCompleted) ?? num(f.runs_completed) ?? 0,
        runsFailed: num(f.runsFailed) ?? num(f.runs_failed) ?? 0,
        runsWithoutResult: num(f.runsWithoutResult) ?? num(f.runs_without_result) ?? 0,
        needsHumanReview: num(f.needsHumanReview) ?? num(f.needs_human_review) ?? 0,
      }
    : null;
  return {
    checkedAt: str(w.checkedAt) ?? str(w.checked_at),
    components,
    funnel,
  };
}
