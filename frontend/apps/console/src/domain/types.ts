/**
 * Domain view models.
 *
 * The console never renders wire DTOs directly. Everything the UI touches is
 * a view model: tolerant of missing fields, explicit about unknown values,
 * and independent of whatever shape the control plane happens to expose today.
 *
 * The backend is being rewritten in parallel; these types are the seam.
 */

export type Severity = "CRITICAL" | "MAJOR" | "MINOR" | "INFO" | "UNKNOWN";

export type StageKind =
  | "QUEUED"
  | "RUNNING"
  | "RETRY_BACKOFF"
  | "NEEDS_DECISION"
  | "DONE"
  | "DEAD"
  | "UNKNOWN";

export type WaitingOn = "WORKER" | "CLOCK" | "PERSON" | "NOBODY" | "UNKNOWN";

export type Verdict = "SUPPORTED" | "REFUTED" | "INCONCLUSIVE" | "UNKNOWN";

export type Lane = "active" | "waiting-person" | "waiting-clock" | "finished" | "failed";

export interface StageVM {
  kind: StageKind;
  rawKind: string;
  waitingOn: WaitingOn;
  rawWaitingOn: string;
  detail: string | null;
  retryable: boolean;
  lane: Lane;
}

export interface IncidentVM {
  id: string;
  service: string;
  title: string;
  severity: Severity;
  rawSeverity: string;
  status: string;
  origin: string;
  createdAt: string;
  updatedAt: string | null;
  stage: StageVM | null;
  assignee: string | null;
  acknowledgedAt: string | null;
  confirmedAt: string | null;
  confirmation: "ACCEPTED" | "REJECTED" | null;
  /** Who signed the confirmation. The backend refuses to rewrite a
   *  confirmation, so the person who signed it is part of the record. */
  confirmedBy: string | null;
  currentRunId: string | null;
  retryAttempts: number | null;
  nextRetryAt: string | null;
  windowStart: string | null;
  windowEnd: string | null;
  duplicateAlerts: number | null;
}

export interface HypothesisVM {
  id: string;
  rank: number | null;
  status: string;
  service: string | null;
  component: string | null;
  faultType: string | null;
  mechanism: string | null;
  predictedObservations: string[];
  supportCount: number;
  refuteCount: number;
}

export interface EvidenceRefVM {
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
  traceId: string | null;
  spanId: string | null;
}

export interface ReportVM {
  runId: string;
  verdict: Verdict;
  rawVerdict: string;
  summary: string;
  abstained: boolean;
  hypotheses: HypothesisVM[];
  evidence: EvidenceRefVM[];
  affectedServices: string[];
  missingData: string[];
  noData: string[];
  rootCauseTime: string | null;
  propagationChain: Array<[string, string]>;
  cost: { toolCalls: number | null; tokens: number | null };
  generatedAt: string | null;
  profileDigest: string | null;
  strategyDigest: string | null;
  rejectedProposals: Array<{ reasonCode: string; proposal: string; detail: string | null }>;
  shadowDecisions: Array<{ candidate: string; note: string | null }>;
  topologyDrift: { declared: number; observed: number; undeclared: number } | null;
}

export interface ProgressStepVM {
  stepIndex: number;
  node: string;
  description: string;
  evidenceCount: number;
  at: string | null;
}

export interface RunVM {
  runId: string;
  startedAt: string | null;
  finishedAt: string | null;
  steps: ProgressStepVM[];
  current: ProgressStepVM | null;
}

export interface ComponentHealthVM {
  name: string;
  status: "UP" | "DEGRADED" | "DOWN" | "UNKNOWN";
  detail: string | null;
  claimedButSilent: boolean;
}

export interface FunnelVM {
  since: string;
  checkedAt: string | null;
  alertsSeen: number;
  alertIncidents: number;
  manualIncidents: number;
  foldedDuplicates: number;
  runsRequested: number;
  runsCompleted: number;
  runsFailed: number;
  runsWithoutResult: number;
  needsHumanReview: number;
}

export interface HealthVM {
  checkedAt: string | null;
  components: ComponentHealthVM[];
  funnel: FunnelVM | null;
}
