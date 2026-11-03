/**
 * Small router: hash-free path routing with a tiny history wrapper.
 *
 * Eight routes, no nesting beyond one level of parameter. A full router library
 * would be more machinery than this surface needs; when the console grows
 * loaders and search-param validation, this is the seam to swap.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

export function usePath() {
  const [path, setPath] = useState(() => window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return path;
}

export function navigate(to: string) {
  if (to === window.location.pathname) return;
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
