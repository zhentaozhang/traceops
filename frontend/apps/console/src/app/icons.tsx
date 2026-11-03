/** Inline icons — 16px grid, 1.5 stroke, currentColor.
 *
 * These are decorative by default: the label next to them carries the meaning,
 * so every icon is `aria-hidden`. Pass `role="img"` + `aria-label` yourself
 * when an icon is the only thing conveying the action.
 */
// biome-ignore-all lint/a11y/noSvgWithoutTitle: every icon here is decorative
// and carries `aria-hidden` from `base()`; the rule cannot see it through a spread
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 15, ...rest }: P) {
  // Decorative unless the caller says otherwise: an icon carrying meaning on
  // its own needs role="img" + aria-label, which then wins over aria-hidden.
  const labelled = rest.role === "img" || rest["aria-label"] !== undefined;
  return {
    width: size,
    height: size,
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": labelled ? undefined : true,
    ...rest,
  };
}

export function Gauge(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M2.5 12a5.5 5.5 0 1 1 11 0" />
      <path d="M8 12l3-4" />
      <circle cx="8" cy="12" r="1.1" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function AlertTriangle(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M8 2.6L14 13H2z" />
      <path d="M8 6.6v3" />
      <circle cx="8" cy="11.2" r=".6" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function Layers(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M8 2.2l5.5 3-5.5 3-5.5-3z" />
      <path d="M2.5 8.4L8 11.4l5.5-3" />
      <path d="M2.5 11.2L8 14.2l5.5-3" />
    </svg>
  );
}

export function Boxes(p: P) {
  return (
    <svg {...base(p)}>
      <rect x="2.2" y="2.2" width="4.6" height="4.6" rx="1" />
      <rect x="9.2" y="2.2" width="4.6" height="4.6" rx="1" />
      <rect x="2.2" y="9.2" width="4.6" height="4.6" rx="1" />
      <rect x="9.2" y="9.2" width="4.6" height="4.6" rx="1" />
    </svg>
  );
}

export function FlaskConical(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M6.4 2v3.2L2.9 11a1.4 1.4 0 0 0 1.2 2.1h7.8a1.4 1.4 0 0 0 1.2-2.1L9.6 5.2V2" />
      <path d="M5.4 2h5.2" />
      <path d="M4.6 9.4h6.8" />
    </svg>
  );
}

export function Activity(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M1.8 8h3l2-5.5 3 11 2-5.5h2.4" />
    </svg>
  );
}

export function Search(p: P) {
  return (
    <svg {...base(p)}>
      <circle cx="7.1" cy="7.1" r="4.4" />
      <path d="M10.4 10.4l3.2 3.2" />
    </svg>
  );
}

export function Sun(p: P) {
  return (
    <svg {...base(p)}>
      <circle cx="8" cy="8" r="3.1" />
      <path d="M8 1.4v1.5M8 13.1v1.5M14.6 8h-1.5M2.9 8H1.4M12.7 3.3l-1 1M4.3 11.7l-1 1M12.7 12.7l-1-1M4.3 4.3l-1-1" />
    </svg>
  );
}

export function Moon(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M13.4 9.6A5.8 5.8 0 0 1 6.4 2.6a5.8 5.8 0 1 0 7 7z" />
    </svg>
  );
}

export function Chevron(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M6 3.5L10.5 8 6 12.5" />
    </svg>
  );
}

export function ArrowRight(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M3 8h10M9 4l4 4-4 4" />
    </svg>
  );
}

export function X(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

export function Copy(p: P) {
  return (
    <svg {...base(p)}>
      <rect x="5.6" y="5.6" width="7.8" height="7.8" rx="1.4" />
      <path d="M10.4 5.6V4a1.4 1.4 0 0 0-1.4-1.4H4A1.4 1.4 0 0 0 2.6 4v5a1.4 1.4 0 0 0 1.4 1.4h1.6" />
    </svg>
  );
}

export function External(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M9.4 2.6H13.4v4" />
      <path d="M13.4 2.6L7.6 8.4" />
      <path d="M12.4 9.6v2.8a1.4 1.4 0 0 1-1.4 1.4H3.6a1.4 1.4 0 0 1-1.4-1.4V5a1.4 1.4 0 0 1 1.4-1.4h2.8" />
    </svg>
  );
}

export function Clock(p: P) {
  return (
    <svg {...base(p)}>
      <circle cx="8" cy="8" r="5.8" />
      <path d="M8 4.8V8l2.2 1.4" />
    </svg>
  );
}

export function User(p: P) {
  return (
    <svg {...base(p)}>
      <circle cx="8" cy="5.6" r="2.6" />
      <path d="M3 13.4a5 5 0 0 1 10 0" />
    </svg>
  );
}

export function Worker(p: P) {
  return (
    <svg {...base(p)}>
      <rect x="3.4" y="5.6" width="9.2" height="6.6" rx="1.4" />
      <path d="M6 5.6V4.4a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1.2" />
      <path d="M6.2 9h3.6" />
    </svg>
  );
}

export function Check(p: P) {
  return (
    <svg {...base(p)}>
      <path d="M3 8.4l3.2 3.2L13 4.8" />
    </svg>
  );
}
