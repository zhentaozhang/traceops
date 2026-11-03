/**
 * The incident status machine.
 *
 * `control-plane/src/main/java/dev/traceops/controlplane/domain/IncidentStatus.java`
 * is the source of truth:
 *
 *     NEW            → { INVESTIGATING, FAILED }
 *     INVESTIGATING  → { DIAGNOSED, FAILED }
 *     DIAGNOSED      → { RESOLVED, FAILED }
 *     RESOLVED       → { }          (terminal)
 *     FAILED         → { }          (terminal)
 *
 * The console mirrors the table instead of asking the server "what may I do
 * next?". The reason is latency of *discovery*: a button that only appears
 * after a round trip is a button nobody finds while an incident is burning.
 * The mirror decides whether to *offer* an action; the server still enforces
 * the same table and is the only thing that can change state. If the two ever
 * disagree, the server wins and the console shows its error.
 *
 * The table is a whitelist, not a blacklist: an unknown status has no
 * outgoing edges, because a status nobody recognises must not be silently
 * treated as "anything goes".
 */

/** The statuses the control plane knows about. */
export type IncidentStatus = "NEW" | "INVESTIGATING" | "DIAGNOSED" | "RESOLVED" | "FAILED";

const STATUSES: IncidentStatus[] = ["NEW", "INVESTIGATING", "DIAGNOSED", "RESOLVED", "FAILED"];

/** Outgoing edges, mirroring `IncidentStatus.ALLOWED`. */
const ALLOWED: Record<IncidentStatus, IncidentStatus[]> = {
  NEW: ["INVESTIGATING", "FAILED"],
  INVESTIGATING: ["DIAGNOSED", "FAILED"],
  DIAGNOSED: ["RESOLVED", "FAILED"],
  RESOLVED: [],
  FAILED: [],
};

/** Human labels. The console speaks Chinese; the API speaks the enum. */
export const STATUS_LABELS: Record<IncidentStatus, string> = {
  NEW: "新建",
  INVESTIGATING: "调查中",
  DIAGNOSED: "已诊断",
  RESOLVED: "已解决",
  FAILED: "已失败",
};

/**
 * What the operator is actually doing when they move an incident to a status.
 * Used as the button label in the transition dialog — "流转到 RESOLVED" is a
 * machine fact, "解决" is a decision.
 */
export const TRANSITION_LABELS: Record<IncidentStatus, string> = {
  NEW: "退回新建",
  INVESTIGATING: "开始调查",
  DIAGNOSED: "标记为已诊断",
  RESOLVED: "解决",
  FAILED: "标记为失败",
};

/**
 * Parse a status string.
 *
 * Returns null for anything unrecognised — the caller keeps the original
 * string for display (see `IncidentVM.status`), because a backend status the
 * console has never heard of is information, not noise.
 */
export function parseIncidentStatus(raw: unknown): IncidentStatus | null {
  if (typeof raw !== "string") return null;
  const upper = raw.trim().toUpperCase();
  return (STATUSES as string[]).includes(upper) ? (upper as IncidentStatus) : null;
}

/**
 * The statuses this incident may move to right now.
 *
 * An unknown or missing status yields an empty list — no reachable targets
 * means the transition control stays disabled rather than offering guesses.
 */
export function allowedTransitions(status: unknown): IncidentStatus[] {
  const parsed = parseIncidentStatus(status);
  if (!parsed) return [];
  return ALLOWED[parsed];
}

/** Whether one specific transition is allowed. */
export function canTransition(from: unknown, to: unknown): boolean {
  const target = parseIncidentStatus(to);
  if (!target) return false;
  return allowedTransitions(from).includes(target);
}

/** True when the status has no outgoing edges. */
export function isTerminalStatus(status: unknown): boolean {
  const parsed = parseIncidentStatus(status);
  if (!parsed) return false;
  return ALLOWED[parsed].length === 0;
}

/**
 * Whether a conclusion can be confirmed right now.
 *
 * The backend refuses a confirmation unless the incident is DIAGNOSED, and
 * refuses a second confirmation outright — a confirmation is a decision, and
 * decisions are not rewritten. Both conditions are checked here so the button
 * is disabled for the right reason instead of failing after the click.
 */
export function canConfirm(status: unknown, confirmation: unknown): boolean {
  return (
    parseIncidentStatus(status) === "DIAGNOSED" &&
    confirmation !== "ACCEPTED" &&
    confirmation !== "REJECTED"
  );
}

/**
 * Whether the incident can be acknowledged right now.
 *
 * Acknowledging a resolved incident is refused by the backend: there is
 * nothing left to pick up.
 */
export function canAcknowledge(status: unknown, acknowledgedAt: unknown): boolean {
  if (parseIncidentStatus(status) === "RESOLVED") return false;
  // Repeat acknowledgements are allowed but do nothing — the first timestamp
  // wins — so the control is only useful before the first one.
  return acknowledgedAt === null || acknowledgedAt === undefined;
}
