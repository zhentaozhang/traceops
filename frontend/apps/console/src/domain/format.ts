/**
 * Presentation helpers. Pure functions over scalars — no backend knowledge.
 */

const TZ = "Asia/Shanghai";

function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** 2026-03-12 14:32:00 */
export function formatInstant(iso: string | null | undefined): string {
  const d = toDate(iso);
  if (!d) return "—";
  const s = new Intl.DateTimeFormat("zh-CN", {
    timeZone: TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
  return s.replace(/\//g, "-");
}

/** 03-12 14:32 — for dense tables */
export function formatInstantShort(iso: string | null | undefined): string {
  const d = toDate(iso);
  if (!d) return "—";
  const s = new Intl.DateTimeFormat("zh-CN", {
    timeZone: TZ,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
  return s.replace(/\//g, "-");
}

/** 3 分钟前 / 2 小时前 */
export function formatRelative(iso: string | null | undefined): string {
  const d = toDate(iso);
  if (!d) return "—";
  const diff = Date.now() - d.getTime();
  const abs = Math.abs(diff);
  const s = Math.round(abs / 1000);
  if (s < 45) return diff >= 0 ? "刚刚" : "即将";
  if (s < 3600) return `${Math.round(s / 60)} 分钟${diff >= 0 ? "前" : "后"}`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时${diff >= 0 ? "前" : "后"}`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} 天${diff >= 0 ? "前" : "后"}`;
  return formatInstantShort(iso);
}

/** 1.4s / 310ms / 2m 05s */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${pad(s)}s`;
}

/** 1.2k / 3.4M */
export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs < 1000) return String(n);
  if (abs < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  if (abs < 1_000_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}G`;
}

/** 1.2k tokens → "12.3k" */
export function formatTokens(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : formatCount(n);
}

/** Percentage from a 0..1 ratio. */
export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return "—";
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function formatWindow(start: string | null, end: string | null): string {
  if (!start && !end) return "—";
  if (start && end) return `${formatInstantShort(start)} → ${formatInstantShort(end)}`;
  return formatInstantShort(start ?? end);
}

/** Shorten a UUID-ish id to "e-9310bce3…4c1e". */
export function shortId(id: string | null | undefined, head = 9, tail = 4): string {
  if (!id) return "—";
  if (id.length <= head + tail + 1) return id;
  return `${id.slice(0, head)}…${id.slice(-tail)}`;
}

/** Trim a checksum for display while keeping it verifiable. */
export function shortHash(hash: string | null | undefined): string {
  if (!hash) return "—";
  return hash.slice(0, 12);
}

/** Human label for a fault type slug: pool_exhaustion → "连接池耗尽" */
const FAULT_LABELS: Record<string, string> = {
  pool_exhaustion: "连接池耗尽",
  upstream_timeout: "上游超时",
  thread_starvation: "线程饥饿",
  error_rate_rise: "错误率上升",
  slow_span: "慢调用",
  cache_miss_storm: "缓存击穿",
  memory_leak: "内存泄漏",
  connection_reset: "连接重置",
  rate_limited: "限流",
  dns_failure: "DNS 失败",
  disk_full: "磁盘写满",
  auth_expiry: "凭据过期",
};

export function faultLabel(slug: string | null | undefined): string {
  if (!slug) return "—";
  return FAULT_LABELS[slug] ?? slug.replace(/_/g, " ");
}

export const EVIDENCE_TYPE_LABELS: Record<string, string> = {
  metric: "指标",
  log: "日志",
  trace: "追踪",
  event: "事件",
  relation: "关系",
  deployment: "部署",
  topology: "拓扑",
};

export function evidenceTypeLabel(t: string | null | undefined): string {
  if (!t) return "—";
  return EVIDENCE_TYPE_LABELS[t] ?? t;
}

export const STAGE_LABELS: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "调查中",
  RETRY_BACKOFF: "退避重试",
  NEEDS_DECISION: "待决策",
  DONE: "已完成",
  DEAD: "已失败",
  UNKNOWN: "未知",
};

export const WAITING_ON_LABELS: Record<string, string> = {
  WORKER: "等 worker",
  CLOCK: "等时间",
  PERSON: "等人",
  NOBODY: "无需处理",
  UNKNOWN: "未知",
};

export const LANE_LABELS: Record<string, string> = {
  "waiting-person": "待人工",
  "waiting-clock": "待重试",
  active: "进行中",
  finished: "已完成",
  failed: "已失败",
};

export const SEVERITY_LABELS: Record<string, string> = {
  CRITICAL: "严重",
  MAJOR: "重要",
  MINOR: "次要",
  INFO: "提示",
  UNKNOWN: "未知",
};

export const VERDICT_LABELS: Record<string, string> = {
  SUPPORTED: "证据支持",
  REFUTED: "已被反驳",
  INCONCLUSIVE: "证据不足",
  UNKNOWN: "结论缺失",
};

export const NODE_LABELS: Record<string, string> = {
  plan: "定范围",
  collect: "取证",
  hypothesize: "提假设",
  verify: "验证",
  rank: "排序",
  report: "出报告",
};
