import { useEffect, useMemo, useRef, useState } from "react";

export interface PickerType {
  id: string;
  name: string;
  description?: string;
  is_enabled?: boolean;
}

interface Props {
  types: PickerType[];
  value: string; // "auto" or a type id
  onChange: (id: string) => void;
  autoLabel?: string;
  autoDescription?: string;
}

const AUTO: PickerType = { id: "auto", name: "Auto-detect" };

/** Searchable single-select for document types.
 *
 * A radio list stops working past a handful of types, and a native <select>
 * hides the description (option tooltips don't show on keyboard or touch) —
 * so this filters as you type and shows each description inline.
 */
export function TypePicker({
  types,
  value,
  onChange,
  autoLabel = "Auto-detect",
  autoDescription = "The AI picks the type (it may match more than one).",
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const options = useMemo<PickerType[]>(
    () => [
      { ...AUTO, name: autoLabel, description: autoDescription },
      ...types.filter((t) => t.is_enabled !== false),
    ],
    [types, autoLabel, autoDescription]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) =>
        o.name.toLowerCase().includes(q) || (o.description || "").toLowerCase().includes(q)
    );
  }, [options, query]);

  const selected = options.find((o) => o.id === value) || options[0];

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation(); // don't also close the surrounding dialog
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(Math.max(0, filtered.findIndex((o) => o.id === value)));
      setTimeout(() => searchRef.current?.focus(), 0);
    }
  }, [open]);

  function choose(id: string) {
    onChange(id);
    setOpen(false);
  }

  function onSearchKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (filtered[active]) choose(filtered[active].id);
    }
  }

  return (
    <div className="type-picker" ref={rootRef}>
      <button
        type="button"
        className={`picker-control${open ? " open" : ""}`}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="picker-value">
          <strong>{selected?.name}</strong>
          {selected?.description && <span className="muted">{selected.description}</span>}
        </span>
        <span className="picker-caret">▾</span>
      </button>

      {open && (
        <div className="picker-menu">
          <input
            ref={searchRef}
            className="picker-search"
            placeholder={`Search ${options.length - 1} document type${options.length - 1 === 1 ? "" : "s"}…`}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onSearchKey}
          />
          <div className="picker-list" role="listbox">
            {filtered.map((o, i) => (
              <div
                key={o.id}
                role="option"
                aria-selected={o.id === value}
                className={
                  "picker-option" +
                  (o.id === value ? " selected" : "") +
                  (i === active ? " active" : "") +
                  (o.id === "auto" ? " auto" : "")
                }
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(o.id)}
              >
                <div className="picker-option-name">
                  {o.name}
                  {o.id === value && <span className="tick">✓</span>}
                </div>
                {o.description && <div className="picker-option-desc">{o.description}</div>}
              </div>
            ))}
            {filtered.length === 0 && (
              <div className="picker-empty muted">No document type matches “{query}”.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
