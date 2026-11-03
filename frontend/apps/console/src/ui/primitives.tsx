/**
 * Small, hand-rolled UI primitives.
 *
 * Every component here takes plain props and knows nothing about the backend.
 */
import { type ReactNode, useState } from "react";
import { shortHash, shortId } from "../domain/format";
import type { Severity, StageKind, Verdict } from "../domain/types";

/* ── button ─────────────────────────────────────────────────────────────── */

type ButtonVariant = "primary" | "default" | "ghost" | "danger";

export function Button({
  variant = "default",
  size = "md",
  children,
  ...rest
}: {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  children: ReactNode;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      {...rest}
      className={`btn btn--${variant} btn--${size} ${rest.className ?? ""}`}
    >
      {children}
    </button>
  );
}

/* ── badge ──────────────────────────────────────────────────────────────── */

export function Badge({
  tone = "neutral",
  children,
  title,
}: {
  tone?: "neutral" | "accent" | "critical" | "major" | "minor" | "info" | "success";
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {children}
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const tone =
    severity === "CRITICAL"
      ? "critical"
      : severity === "MAJOR"
        ? "major"
        : severity === "MINOR"
          ? "minor"
          : severity === "INFO"
            ? "info"
            : "neutral";
  return <Badge tone={tone}>{severity}</Badge>;
}

export function VerdictBadge({
  verdict,
  abstained,
  label,
}: {
  verdict: Verdict;
  abstained?: boolean;
  label: string;
}) {
  if (abstained) {
    return (
      <Badge tone="info" title="证据不足时主动弃权，是一种正确的结论">
        <span className="badge__dot" style={{ background: "var(--verdict-abstained)" }} />
        弃权
      </Badge>
    );
  }
  const tone =
    verdict === "SUPPORTED" ? "success" : verdict === "REFUTED" ? "critical" : "neutral";
  return (
    <Badge tone={tone}>
      <span
        className="badge__dot"
        style={{
          background:
            verdict === "SUPPORTED"
              ? "var(--verdict-supported)"
              : verdict === "REFUTED"
                ? "var(--verdict-refuted)"
                : "var(--verdict-inconclusive)",
        }}
      />
      {label}
    </Badge>
  );
}

export function StageBadge({
  kind,
  label,
  running,
}: {
  kind: StageKind;
  label: string;
  running?: boolean;
}) {
  const color =
    kind === "RUNNING"
      ? "var(--stage-running)"
      : kind === "NEEDS_DECISION"
        ? "var(--stage-decision)"
        : kind === "RETRY_BACKOFF"
          ? "var(--stage-backoff)"
          : kind === "DONE"
            ? "var(--stage-done)"
            : kind === "DEAD"
              ? "var(--stage-dead)"
              : "var(--stage-queued)";
  return (
    <span className="stage-badge">
      <span
        className={`stage-badge__dot${running ? " is-pulsing" : ""}`}
        style={{ background: color }}
      />
      {label}
    </span>
  );
}

/* ── panels ─────────────────────────────────────────────────────────────── */

export function Panel({
  title,
  meta,
  aside,
  children,
  className,
}: {
  title?: ReactNode;
  meta?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className ?? ""}`}>
      {(title || aside || meta) && (
        <header className="panel__head">
          {title && <h3 className="panel__title">{title}</h3>}
          {meta && <span className="panel__meta">{meta}</span>}
          {aside && <div className="panel__aside">{aside}</div>}
        </header>
      )}
      <div className="panel__body">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "critical" | "accent" | "success" | "neutral";
}) {
  return (
    <div className={`stat${tone ? ` stat--${tone}` : ""}`}>
      <div className="stat__label">{label}</div>
      <div className="stat__value num">{value}</div>
      {hint && <div className="stat__hint">{hint}</div>}
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty__mark" aria-hidden="true">
        ⌀
      </div>
      <div className="empty__title">{title}</div>
      {detail && <div className="empty__detail">{detail}</div>}
      {action && <div className="empty__action">{action}</div>}
    </div>
  );
}

export function ErrorState({ detail }: { detail?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty__mark empty__mark--danger" aria-hidden="true">
        !
      </div>
      <div className="empty__title">取不到数据</div>
      <div className="empty__detail">
        {detail ?? "上游服务没有返回可解析的内容。稍后重试，或查看 /ops 的组件健康。"}
      </div>
    </div>
  );
}

/* ── data display ───────────────────────────────────────────────────────── */

export function KeyValue({ items }: { items: Array<{ label: string; value: ReactNode }> }) {
  return (
    <dl className="kv">
      {items.map((item) => (
        <div className="kv__row" key={item.label}>
          <dt className="kv__k">{item.label}</dt>
          <dd className="kv__v">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Copyable({
  value,
  display,
  mono = true,
}: {
  value: string;
  display?: ReactNode;
  mono?: boolean;
}) {
  return (
    <button
      type="button"
      className={`copyable${mono ? " mono" : ""}`}
      title={value}
      onClick={() => {
        void navigator.clipboard?.writeText(value);
      }}
    >
      {display ?? shortId(value)}
    </button>
  );
}

export function Hash({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="faint">—</span>;
  return (
    <span className="mono" title={value}>
      {shortHash(value)}
    </span>
  );
}

/* ── json viewer ────────────────────────────────────────────────────────── */

/**
 * A JSON viewer that folds deep structures away.
 *
 * Evidence payloads are large and mostly boring past the first couple of
 * levels — a folded node keeps the key visible so the shape is still
 * legible, while the value stays one click away.
 */
export function JsonViewer({
  value,
  collapsedDepth = 2,
  maxHeight = 420,
}: {
  value: unknown;
  collapsedDepth?: number;
  maxHeight?: number;
}) {
  return (
    <div className="json" style={{ maxHeight }}>
      <JsonNode value={value} depth={0} collapseAfter={collapsedDepth} />
    </div>
  );
}

function JsonNode({
  value,
  depth,
  collapseAfter,
}: {
  value: unknown;
  depth: number;
  collapseAfter: number;
}) {
  // A node past the fold depth starts closed: the shape stays legible, the
  // payload stays one click away.
  const foldable = depth >= collapseAfter;
  const [open, setOpen] = useState(!foldable);

  if (value === null) return <span className="json__null">null</span>;
  if (value === undefined) return <span className="json__null">null</span>;
  if (typeof value === "boolean") return <span className="json__bool">{String(value)}</span>;
  if (typeof value === "number") return <span className="json__num">{String(value)}</span>;
  if (typeof value === "string") return <span className="json__str">&quot;{value}&quot;</span>;

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="json__empty">[ ]</span>;
    const expanded = open || !foldable;
    return (
      <span className="json__node">
        <button
          type="button"
          className="json__toggle"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={expanded}
          hidden={!foldable}
        >
          {expanded ? "▾" : "▸"}
        </button>
        <span className="json__punct">[</span>
        {expanded && (
          <span className="json__children">
            {value.map((item, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: JSON arrays are positional — a value may legitimately repeat, so there is no stable id to key on
              <span className="json__row" key={i}>
                <span className="json__comma">{i > 0 ? "," : ""}</span>
                <JsonNode value={item} depth={depth + 1} collapseAfter={collapseAfter} />
              </span>
            ))}
          </span>
        )}
        {!expanded && <span className="json__hint">{value.length} 项</span>}
        <span className="json__punct">]</span>
      </span>
    );
  }

  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <span className="json__empty">{`{ }`}</span>;
  const expanded = open || !foldable;
  return (
    <span className="json__node">
      <button
        type="button"
        className="json__toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={expanded}
        hidden={!foldable}
      >
        {expanded ? "▾" : "▸"}
      </button>
      <span className="json__punct">{`{`}</span>
      {expanded && (
        <span className="json__children">
          {entries.map(([k, v], i) => (
            <span className="json__row" key={k}>
              <span className="json__comma">{i > 0 ? "," : ""}</span>
              <span className="json__key">{k}</span>
              <span className="json__punct">:</span>{" "}
              <JsonNode value={v} depth={depth + 1} collapseAfter={collapseAfter} />
            </span>
          ))}
        </span>
      )}
      {!expanded && <span className="json__hint">{entries.length} 个字段</span>}
      <span className="json__punct">{`}`}</span>
    </span>
  );
}

/* ── progress ───────────────────────────────────────────────────────────── */

export function Meter({
  value,
  max = 1,
  tone = "accent",
}: {
  value: number;
  max?: number;
  tone?: "accent" | "success" | "critical";
}) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div className="meter" role="img" aria-label={`${pct.toFixed(0)}%`}>
      <div className={`meter__fill meter__fill--${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}
