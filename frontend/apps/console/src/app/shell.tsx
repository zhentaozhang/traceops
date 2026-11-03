/**
 * Application shell: rail, topbar, command palette, theme & density controls.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { dataSource } from "../data/source";
import { LANE_LABELS } from "../domain/format";
import type { IncidentVM } from "../domain/types";
import { useFocusTrap } from "../ui/overlay";
import {
  Activity,
  AlertTriangle,
  Boxes,
  FlaskConical,
  Gauge,
  Layers,
  Moon,
  Search,
  Sun,
} from "./icons";
import { Link } from "./link";

/* ── theme ──────────────────────────────────────────────────────────────── */

export type Theme = "dark" | "light";
export type Density = "comfortable" | "compact";

const THEME_KEY = "traceops.theme";
const DENSITY_KEY = "traceops.density";

/**
 * Theme and density are operator preferences, not decoration — an on-call
 * engineer who picked compact rows at 3am should not have to pick them again
 * after a reload. The DOM dataset is the source of truth at runtime; local
 * storage carries it across reloads.
 */
function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = readPreference(THEME_KEY);
    if (stored === "dark" || stored === "light") return stored;
    return (document.documentElement.dataset.theme as Theme) ?? "dark";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    writePreference(THEME_KEY, theme);
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))] as const;
}

function useDensity() {
  const [density, setDensity] = useState<Density>(() => {
    const stored = readPreference(DENSITY_KEY);
    if (stored === "comfortable" || stored === "compact") return stored;
    return (document.documentElement.dataset.density as Density) ?? "comfortable";
  });
  useEffect(() => {
    document.documentElement.dataset.density = density;
    writePreference(DENSITY_KEY, density);
  }, [density]);
  return [
    density,
    () => setDensity((d) => (d === "comfortable" ? "compact" : "comfortable")),
  ] as const;
}

function readPreference(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Private mode or a blocked origin — fall back to the document default.
    return null;
  }
}

function writePreference(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Preference is a nicety; never let it break the console.
  }
}

/* ── navigation ─────────────────────────────────────────────────────────── */

export interface NavEntry {
  to: string;
  label: string;
  icon: typeof Gauge;
  count?: number;
  match?: (path: string) => boolean;
}

export function Rail({ path, incidents }: { path: string; incidents: IncidentVM[] }) {
  const pending = incidents.filter((i) => i.stage?.lane === "waiting-person").length;
  const active = incidents.filter((i) => i.stage?.lane === "active").length;

  const groups: Array<{ label: string; items: NavEntry[] }> = [
    {
      label: "值班",
      items: [
        {
          to: "/",
          label: "概览",
          icon: Gauge,
          match: (p) => p === "/" || p === "",
        },
        {
          to: "/incidents",
          label: "事故",
          icon: AlertTriangle,
          count: incidents.length,
          match: (p) => p.startsWith("/incidents"),
        },
        {
          to: "/evidence",
          label: "证据",
          icon: Layers,
          match: (p) => p.startsWith("/evidence"),
        },
      ],
    },
    {
      label: "系统",
      items: [
        {
          to: "/topology",
          label: "拓扑",
          icon: Boxes,
          match: (p) => p.startsWith("/topology"),
        },
        {
          to: "/evaluation",
          label: "评测",
          icon: FlaskConical,
          match: (p) => p.startsWith("/evaluation"),
        },
        {
          to: "/ops",
          label: "运维",
          icon: Activity,
          match: (p) => p.startsWith("/ops"),
        },
      ],
    },
  ];

  return (
    <nav className="rail" aria-label="主导航">
      <div className="rail__brand">
        <span className="rail__glyph" aria-hidden="true">
          <svg width="17" height="17" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path
              d="M2.5 12.5 L6 6.5 L9 12.5 L12 4.5 L17.5 12.5"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <circle cx="12" cy="4.5" r="1.9" fill="currentColor" />
          </svg>
        </span>
        <span className="rail__word">
          TraceOps <em>/ console</em>
        </span>
      </div>

      <div className="rail__nav">
        {groups.map((g) => (
          <div className="rail__group" key={g.label}>
            <span className="label">{g.label}</span>
            {g.items.map((item) => {
              const Icon = item.icon;
              const isActive = item.match ? item.match(path) : path === item.to;
              const count = item.to === "/incidents" && active > 0 ? active : item.count;
              // A real anchor, not a button: the browser then gives the
              // operator middle-click, Cmd/Ctrl-click, "copy link address"
              // and a status-bar preview for free.
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className={`nav__item${isActive ? " is-active" : ""}`}
                  aria-current={isActive ? "page" : undefined}
                >
                  <Icon />
                  <span>{item.label}</span>
                  {count !== undefined && count > 0 && (
                    <span className="nav__count">{count}</span>
                  )}
                </Link>
              );
            })}
          </div>
        ))}
      </div>

      <div className="rail__foot">
        <strong>{pending} 起待人工</strong>
        {active} 起调查中
        <br />
        <span className="faint">LANE 优先级</span>
      </div>
    </nav>
  );
}

/* ── topbar ─────────────────────────────────────────────────────────────── */

export function Topbar({
  crumbs,
  onOpenPalette,
}: {
  crumbs: Array<{ label: string; to?: string }>;
  onOpenPalette: () => void;
}) {
  const [theme, toggleTheme] = useTheme();
  const [density, toggleDensity] = useDensity();

  return (
    <header className="topbar">
      <nav className="crumbs" aria-label="面包屑">
        {crumbs.map((c, i) => (
          <span key={c.to ?? c.label} className="crumbs__item">
            {i > 0 && <span className="crumbs__sep">/</span>}
            {c.to ? (
              <Link to={c.to}>{c.label}</Link>
            ) : (
              <span className="crumbs__here">{c.label}</span>
            )}
          </span>
        ))}
      </nav>

      <div className="topbar__right">
        <button type="button" className="search-trigger" onClick={onOpenPalette}>
          <Search />
          <span>搜索</span>
          <span className="kbd">⌘ K</span>
        </button>
        <div className="topbar__divider" />
        <button
          type="button"
          className="ghost-btn"
          onClick={toggleDensity}
          title={`密度：${density === "comfortable" ? "舒适" : "紧凑"}`}
          aria-label="切换密度"
        >
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "var(--text-2xs)",
              letterSpacing: "0.06em",
            }}
          >
            {density === "comfortable" ? "AA" : "A"}
          </span>
        </button>
        <button
          type="button"
          className="ghost-btn"
          onClick={toggleTheme}
          title={theme === "dark" ? "切换到浅色" : "切换到深色"}
          aria-label="切换主题"
        >
          {theme === "dark" ? <Sun /> : <Moon />}
        </button>
      </div>
    </header>
  );
}

/* ── command palette ────────────────────────────────────────────────────── */

interface Command {
  id: string;
  label: string;
  group: string;
  hint?: string;
  run: () => void;
}

export function CommandPalette({
  commands,
  onClose,
}: {
  commands: Command[];
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const paletteRef = useRef<HTMLDivElement>(null);

  // The palette is modal: the page behind it must not be Tab-reachable, and
  // closing it must return the keyboard to whatever opened it.
  useFocusTrap(paletteRef, true, { autoFocus: false });

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) => c.label.toLowerCase().includes(q) || c.group.toLowerCase().includes(q),
    );
  }, [commands, query]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setCursor((c) => Math.min(c + 1, filtered.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setCursor((c) => Math.max(c - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const cmd = filtered[cursor];
        if (cmd) {
          cmd.run();
          onClose();
        }
      }
    },
    [filtered, cursor, onClose],
  );

  let lastGroup = "";

  return (
    <div
      className="overlay"
      role="dialog"
      aria-modal="true"
      aria-label="命令面板"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="palette" ref={paletteRef}>
        <div className="palette__input">
          <Search />
          <input
            // A command palette is useless without the caret in it — the whole
            // interaction is typing, then arrowing through matches.
            // biome-ignore lint/a11y/noAutofocus: focus belongs here on open
            autoFocus
            value={query}
            name="command-palette-query"
            autoComplete="off"
            spellCheck={false}
            placeholder="搜索命令、事故、服务…"
            onChange={(e) => {
              setQuery(e.target.value);
              // A new query means a new list — start at the top again.
              setCursor(0);
            }}
            onKeyDown={onKeyDown}
            aria-label="命令输入"
          />
          <span className="kbd">esc</span>
        </div>
        <div className="palette__list">
          {filtered.length === 0 && <div className="palette__empty">没有匹配的命令</div>}
          {filtered.map((cmd, i) => {
            const showGroup = cmd.group !== lastGroup;
            lastGroup = cmd.group;
            return (
              <div key={cmd.id}>
                {showGroup && <div className="palette__group">{cmd.group}</div>}
                <button
                  type="button"
                  className="palette__item"
                  data-active={i === cursor}
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => {
                    cmd.run();
                    onClose();
                  }}
                >
                  <span>{cmd.label}</span>
                  {cmd.hint && <span className="palette__hint">{cmd.hint}</span>}
                </button>
              </div>
            );
          })}
        </div>
        <div className="palette__foot">
          <span>↑↓ 选择</span>
          <span>↵ 执行</span>
          <span>esc 关闭</span>
        </div>
      </div>
    </div>
  );
}

/* ── incident search index (fed into the palette) ───────────────────────── */

export function useIncidentCommands(
  incidents: IncidentVM[],
  onNavigate: (to: string) => void,
): Command[] {
  return useMemo(
    () =>
      incidents.map((i) => ({
        id: `incident-${i.id}`,
        label: `${i.service} · ${i.title}`,
        group: "事故",
        hint: i.id,
        run: () => onNavigate(`/incidents/${i.id}`),
      })),
    [incidents, onNavigate],
  );
}

export { dataSource };
export const laneLabels = LANE_LABELS;
