import { ReactNode, useState } from "react";

interface Props {
  title: string;
  badge?: ReactNode;
  /** extra header buttons (e.g. expand) — clicks don't toggle the section */
  actions?: ReactNode;
  defaultOpen?: boolean;
  /** Controlled open state (e.g. a page's "Focus mode" forcing sections
   * open/closed). When omitted, the section manages its own state, seeded
   * from defaultOpen, same as before. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}

/** A panel section that collapses to its header. Open sections share the
 * side-panel's height (flex) and scroll internally. */
export function CollapsibleSection({
  title, badge, actions, defaultOpen = true, open: openProp, onOpenChange, children,
}: Props) {
  const [openState, setOpenState] = useState(defaultOpen);
  const open = openProp !== undefined ? openProp : openState;

  function toggle() {
    const next = !open;
    if (openProp === undefined) setOpenState(next);
    onOpenChange?.(next);
  }

  return (
    <div className={`section${open ? " open" : ""}`}>
      <div className="section-head" onClick={toggle} title={open ? "Collapse" : "Expand"}>
        <span className="chev">{open ? "▼" : "▶"}</span>
        <strong>{title}</strong>
        {badge}
        <span className="section-actions" onClick={(e) => e.stopPropagation()}>
          {actions}
        </span>
      </div>
      {open && <div className="section-body">{children}</div>}
    </div>
  );
}
