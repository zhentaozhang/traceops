/**
 * Link interception.
 *
 * The console renders navigation as real `<a href>` elements so that the
 * browser's own affordances work: middle-click opens a new tab, Cmd/Ctrl+click
 * opens a new tab in the background, "copy link address" copies a URL that
 * actually resolves, and the status bar shows the destination.
 *
 * But the console is a SPA with a hand-rolled history wrapper, so a plain
 * `<a href>` would reload the whole app — losing the in-memory incident
 * index and the open command palette. So: render a real anchor, and
 * intercept only the clicks that a SPA should handle (plain left click),
 * leaving every modified click to the browser.
 *
 * This is the same rule TanStack Router applies in its `Link` component, and
 * it is why the audit found "navigation as `<button onClick>`" to be a real
 * defect rather than a style preference.
 */
import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";
import { navigate } from "./router";

/**
 * Should this click be handled in-app?
 *
 * Modified clicks (⌘/Ctrl/Shift/Alt, middle button) belong to the browser:
 * they mean "open somewhere else", and intercepting them would break the
 * user's intent.
 */
function isPlainLeftClick(event: MouseEvent<HTMLAnchorElement>): boolean {
  if (event.defaultPrevented) return false;
  if (event.button !== 0) return false;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
  return true;
}

/**
 * An in-app link.
 *
 * `to` is a path with an optional query string — the same shape the
 * `navigate()` helper accepts.
 */
export function Link({
  to,
  children,
  onClick,
  ...rest
}: {
  to: string;
  children: ReactNode;
} & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return (
    <a
      href={to}
      {...rest}
      onClick={(event) => {
        onClick?.(event);
        if (!isPlainLeftClick(event)) return;
        // Let the caller opt out (e.g. a link that opens a report elsewhere).
        if (event.defaultPrevented) return;
        if (rest.target && rest.target !== "_self") return;
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

/**
 * A link whose label is an identifier rather than prose.
 *
 * Service names, run ids and checksums must not be machine-translated: a
 * browser that turns "checkout-service" into "结账-服务" has corrupted a
 * string the operator is about to paste into a log query.
 */
export function IdentLink({
  to,
  children,
  ...rest
}: {
  to: string;
  children: ReactNode;
} & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return (
    <Link to={to} translate="no" {...rest}>
      {children}
    </Link>
  );
}
