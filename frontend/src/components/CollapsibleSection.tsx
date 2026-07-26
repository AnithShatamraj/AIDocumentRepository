import { ReactNode, useState } from "react";

interface Props {
  title: string;
  badge?: ReactNode;
  /** extra header buttons (e.g. expand) — clicks don't toggle the section */
  actions?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}

/** A panel section that collapses to its header. Open sections share the
 * side-panel's height (flex) and scroll internally. */
export function CollapsibleSection({ title, badge, actions, defaultOpen = true, children }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`section${open ? " open" : ""}`}>
      <div className="section-head" onClick={() => setOpen((o) => !o)} title={open ? "Collapse" : "Expand"}>
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
