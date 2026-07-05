import { useEffect, useRef } from "react";
import { FOCUSABLE_SELECTOR, isEscape, tabWrapTarget } from "./modalFocus";

// Modal.tsx — the shared a11y dialog shell (P31/UX-12). One primitive gives every modal the same baseline:
//   • Esc-to-close and backdrop click-to-close,
//   • a focus trap (Tab / Shift+Tab cycle within the dialog),
//   • initial focus on open + focus RESTORE to the opener on close,
//   • role="dialog" + aria-modal + aria-labelledby.
// It deliberately does NOT dictate the modal's look: each consumer keeps its own card/panel via
// `containerStyle`, and the backdrop family (opaque overlay vs translucent scrim) is chosen by
// `backdropStyle`. Omit `backdropStyle` for the non-scrim side drawer (ThemeCustomize) — Esc / trap / role
// still apply, but there is no overlay and no click-out. The pure key decisions live in ./modalFocus (unit
// tested in node); the DOM wiring here is exercised by the manual / headless a11y pass.

type ModalProps = {
  // Called on Esc, on a backdrop click, and by the consumer's own close control. Omit only for a
  // non-dismissible primary surface (CasePicker's empty state renders WITHOUT Modal in that mode).
  onClose: () => void;
  // The consumer's card / panel style (becomes the role="dialog" element).
  containerStyle: React.CSSProperties;
  // The overlay / scrim style. Omit -> no backdrop element (side-drawer mode, no click-out, no aria-modal).
  backdropStyle?: React.CSSProperties;
  // id of the element that titles the dialog (the consumer puts the same id on its heading) -> aria-labelledby.
  labelledBy?: string;
  // Optional control to focus on open (e.g. the address field). Falls back to the first focusable control.
  initialFocus?: React.RefObject<HTMLElement | null>;
  // Backdrop click closes the modal (default true; only relevant when backdropStyle is present).
  closeOnBackdrop?: boolean;
  children: React.ReactNode;
};

export default function Modal({
  onClose, containerStyle, backdropStyle, labelledBy, initialFocus,
  closeOnBackdrop = true, children,
}: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // On open: remember the opener, then move focus into the dialog (explicit target, else the first control,
  // else the container). On close (unmount): restore focus to the opener so keyboard/AT users land back where
  // they were. If React's autoFocus already placed focus inside, leave it.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const dlg = dialogRef.current;
    const explicit = initialFocus?.current ?? null;
    if (explicit) {
      explicit.focus();
    } else {
      const inside = dlg && document.activeElement instanceof Node
        && document.activeElement !== document.body && dlg.contains(document.activeElement);
      if (!inside) (dlg?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ?? dlg)?.focus();
    }
    return () => { opener?.focus?.(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (isEscape(e.key)) { e.stopPropagation(); onClose(); return; }
    if (e.key !== "Tab") return;
    const dlg = dialogRef.current;
    if (!dlg) return;
    // Visible, tabbable controls in DOM order (offsetParent === null filters display:none descendants).
    const nodes = Array.from(dlg.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter((el) => el.offsetParent !== null || el === document.activeElement);
    const active = document.activeElement as HTMLElement | null;
    const idx = active ? nodes.indexOf(active) : -1;
    const target = tabWrapTarget(nodes.length, idx, e.shiftKey);
    if (target != null) { e.preventDefault(); nodes[target]?.focus(); }
  };

  const dialog = (
    <div ref={dialogRef} role="dialog" aria-modal={backdropStyle ? true : undefined}
         aria-labelledby={labelledBy} tabIndex={-1} style={containerStyle} onKeyDown={onKeyDown}>
      {children}
    </div>
  );

  // Side-drawer mode: no backdrop element, but the dialog keeps Esc / focus-trap / role.
  if (!backdropStyle) return dialog;

  return (
    <div style={backdropStyle} role="presentation"
         onClick={closeOnBackdrop ? (e) => { if (e.target === e.currentTarget) onClose(); } : undefined}>
      {dialog}
    </div>
  );
}
