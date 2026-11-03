/**
 * Small router: hash-free path routing with a tiny history wrapper.
 *
 * Eight routes, no nesting beyond one level of parameter. A full router library
 * would be more machinery than this surface needs; when the console grows
 * loaders and search-param validation, this is the seam to swap.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

/**
 * The current location.
 *
 * `pathname` is what routes match on; `search` is the query string. Both are
 * tracked because a link that changes only the filters still has to re-render
 * — a list filtered by `?service=checkout-service` is a different list.
 */
export interface Location {
  pathname: string;
  search: string;
  hash: string;
}

function readLocation(): Location {
  return {
    pathname: window.location.pathname,
    search: window.location.search,
    hash: window.location.hash,
  };
}

export function useLocation(): Location {
  const [location, setLocation] = useState(readLocation);
  useEffect(() => {
    const onPop = () => setLocation(readLocation());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return location;
}

/** The current pathname. */
export function usePath(): string {
  return useLocation().pathname;
}

/**
 * Navigate in-app.
 *
 * The full URL (path + query + hash) is pushed, so a link to the same page
 * with different filters still works. Comparing against the whole URL rather
 * than just the pathname is what makes "filter this list" shareable.
 */
export function navigate(to: string) {
  const current = window.location.pathname + window.location.search + window.location.hash;
  if (to === current) return;
  window.history.pushState({}, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function useNavigate() {
  return useCallback((to: string) => navigate(to), []);
}

/**
 * Match a pathname against patterns like `/incidents/:id/runs/:runId`.
 * Returns the extracted params, or null when the pattern does not apply.
 */
export function matchPath(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split("/").filter(Boolean);
  const s = path.split("/").filter(Boolean);
  if (p.length !== s.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    const seg = p[i] as string;
    const actual = s[i] as string;
    if (seg.startsWith(":")) {
      params[seg.slice(1)] = decodeURIComponent(actual);
    } else if (seg !== actual) {
      return null;
    }
  }
  return params;
}

export function useRoute(patterns: Array<[string, string]>) {
  const path = usePath();
  const navigate = useNavigate();
  return useMemo(() => {
    for (const [pattern, key] of patterns) {
      const params = matchPath(pattern, path);
      if (params) return { key, params, path, navigate };
    }
    return { key: "not-found", params: {}, path, navigate };
  }, [path, patterns, navigate]);
}
