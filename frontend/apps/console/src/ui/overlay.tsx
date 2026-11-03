/**
 * Overlay primitives: focus trap, focus restoration, modal shell.
 *
 * Every overlay in this console (command palette, evidence drawer, action
 * dialogs) is a *modal surface*: the page behind it must not be reachable by
 * Tab, and when the overlay closes the keyboard must land back where it
 * started. Without that, an operator who opens a dialog with the keyboard
 * ends up tabbing through the whole page behind it, and closing the dialog
 * drops focus on `<body>` — so the next Tab starts from the top of the app.
 *
 * The trap is implemented here rather than per-overlay so the behaviour is
 * identical everywhere and has one place to be tested.
 */
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";

/** Elements that can hold keyboard focus, in DOM order. */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => {
    // A hidden element has no client rects; a visible one inside a
    // `position: fixed` ancestor still reports them.
    if (el.getClientRects().length === 0) return false;
    return !el.hasAttribute("inert");
  });
}

/**
 * Trap keyboard focus inside `ref` while `active`.
 *
 * On activation the first focusable element inside the overlay receives focus
 * (or the container itself, if it has none). On deactivation focus returns to
 * whatever was focused before — that is the "focus restoration" half of the
 * contract, and it is the half that is easy to forget.
 */
export function useFocusTrap(
  ref: React.RefObject<HTMLElement | null>,
  active: boolean,
  options: { autoFocus?: boolean } = {},
) {
  const { autoFocus = true } = options;
  const restoreTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;
    const root = ref.current;
    if (!root) return;

    restoreTo.current = document.activeElement as HTMLElement | null;

    if (autoFocus) {
      const first = focusableWithin(root)[0];
      (first ?? root).focus();
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const items = focusableWithin(root);
      if (items.length === 0) {
        // Nothing to move to — keep focus on the container rather than
        // letting it escape to the page behind.
        event.preventDefault();
        root.focus();
        return;
      }
      const first = items[0] as HTMLElement;
      const last = items[items.length - 1] as HTMLElement;
      const activeEl = document.activeElement as HTMLElement | null;
      const inside = activeEl ? root.contains(activeEl) : false;

      if (!inside) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (event.shiftKey && activeEl === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && activeEl === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      // Return focus to where it came from. `preventScroll` keeps the page from
      // jumping back to a position the user has since scrolled away from.
      restoreTo.current?.focus?.({ preventScroll: true });
      restoreTo.current = null;
    };
  }, [active, autoFocus, ref]);
}

/**
 * Close on Escape.
 *
 * Kept separate from the trap because the two have different lifetimes: a
 * dialog may want Escape while the trap is inactive (e.g. during a
 * confirmation step inside the same overlay).
 */
export function useEscape(active: boolean, onEscape: () => void) {
  const handler = useRef(onEscape);
  handler.current = onEscape;

  useEffect(() => {
    if (!active) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        handler.current();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [active]);
}

/**
 * A modal dialog shell.
 *
 * `aria-modal="true"` tells assistive technology the content behind is
 * inert; the focus trap is what actually enforces it for keyboard users.
 */
export function Modal({
  title,
  description,
  onClose,
  children,
  footer,
  className,
}: {
  title: string;
  description?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useFocusTrap(panelRef, true);
  useEscape(true, onClose);

  return (
    <div className="overlay overlay--dialog">
      {/* The backdrop is a real button: clicking outside a dialog is how
          people dismiss it, and a control that only exists for the mouse
          is a control the keyboard and screen-reader users do not have.
          The focus trap keeps the tab order inside the dialog, so this
          button never becomes a stray tab stop. */}
      <button
        type="button"
        className="overlay__backdrop"
        aria-label="关闭对话框"
        onClick={onClose}
      />
      <div
        className={`modal ${className ?? ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={panelRef}
        tabIndex={-1}
      >
        <div className="modal__head">
          <h2 className="modal__title" id={titleId}>
            {title}
          </h2>
          <button
            type="button"
            className="ghost-btn"
            onClick={onClose}
            aria-label="关闭对话框"
            title="关闭（Esc）"
          >
            <CloseIcon />
          </button>
        </div>
        <div className="modal__body">
          {description && <p className="modal__desc">{description}</p>}
          {children}
        </div>
        {footer && <div className="modal__foot">{footer}</div>}
      </div>
    </div>
  );
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <path
        d="M3 3 L11 11 M11 3 L3 11"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** A labelled form field. */
export function Field({
  label,
  hint,
  children,
  id,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  id: string;
}) {
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {children}
      {hint && <p className="field__hint">{hint}</p>}
    </div>
  );
}

/**
 * Run an async action with a pending flag and an error slot.
 *
 * The `pending` guard is what stops a double-submit: the backend rejects a
 * second confirmation ("a confirmation cannot be rewritten"), so a double
 * click would otherwise produce a confusing error after a success.
 */
export function useAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (fn: () => Promise<void>) => {
      if (pending) return;
      setPending(true);
      setError(null);
      try {
        await fn();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setPending(false);
      }
    },
    [pending],
  );

  return { pending, error, setError, run };
}
