import { ReactNode, useEffect, useRef, useState } from "react";

/** A funnel button for a table header that opens a popover holding whatever
 *  controls suit the column's data type. The button is highlighted while the
 *  filter is doing something, so an active filter is visible from the header
 *  even with the popover closed. */
export function FilterPopover({
  label, active, onClear, align = "left", wide = false, children,
}: {
  label: string;
  active: boolean;
  onClear: () => void;
  align?: "left" | "right";
  wide?: boolean; // room for a calendar
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  // Click outside or Escape closes it. Listeners exist only while it's open.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span className="col-filter" ref={ref}>
      <button
        type="button"
        className={`filter-btn${active ? " on" : ""}`}
        aria-label={`Filter by ${label}`}
        aria-expanded={open}
        title={active ? `Filtering by ${label}` : `Filter by ${label}`}
        onClick={() => setOpen((o) => !o)}
      >
        <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path d="M1.5 2h13L9.6 8.2V13l-3.2 1.6V8.2z" />
        </svg>
      </button>
      {open && (
        <div className={`filter-pop ${align}${wide ? " wide" : ""}`} role="dialog" aria-label={`${label} filter`}>
          <div className="filter-pop-title">{label}</div>
          {children}
          <div className="filter-pop-foot">
            <button type="button" className="ghost tiny" disabled={!active} onClick={onClear}>Clear</button>
            <button type="button" className="tiny" onClick={() => setOpen(false)}>Done</button>
          </div>
        </div>
      )}
    </span>
  );
}

export interface CheckOption {
  value: string;
  label: string;
}

/** Multi-select as a scrolling checkbox list; an empty selection means "no
 *  restriction", which is what every caller treats it as. */
export function CheckList({
  options, value, onChange, empty = "No values to choose from.",
}: {
  options: CheckOption[];
  value: string[];
  onChange: (next: string[]) => void;
  empty?: string;
}) {
  if (!options.length) return <div className="muted tiny-text">{empty}</div>;
  const toggle = (v: string) =>
    onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v]);
  return (
    <div className="check-list">
      {options.map((o) => (
        <label key={o.value} className="check-row">
          <input type="checkbox" checked={value.includes(o.value)} onChange={() => toggle(o.value)} />
          <span>{o.label}</span>
        </label>
      ))}
    </div>
  );
}
