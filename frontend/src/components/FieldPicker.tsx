import { useEffect, useMemo, useRef, useState } from "react";

export interface SearchableField {
  field_key: string;
  path_pattern: string; // what's actually sent to the API — [] means "any list index"
  label: string;
  data_type: string;
  repeats: boolean;
}

interface Props {
  fields: SearchableField[];
  value: string | null; // selected path_pattern, or null
  onChange: (field: SearchableField | null) => void;
  placeholder?: string;
}

/** Searchable single-select for a structured-search field.
 *
 * Fields can be nested/repeating (`work_experience[].organization`), so a
 * flat text input can't express "any item in this list" — this replaces free
 * text with a pick-from-schema list, filterable by name.
 */
export function FieldPicker({ fields, value, onChange, placeholder = "Search fields…" }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return fields;
    return fields.filter(
      (f) => f.label.toLowerCase().includes(q) || f.field_key.toLowerCase().includes(q)
    );
  }, [fields, query]);

  const selected = fields.find((f) => f.path_pattern === value) || null;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
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
      setActive(0);
      setTimeout(() => searchRef.current?.focus(), 0);
    }
  }, [open]);

  function choose(f: SearchableField | null) {
    onChange(f);
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
      if (filtered[active]) choose(filtered[active]);
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
          {selected ? (
            <>
              <strong>{selected.label}</strong>
              <span className="muted">
                {selected.data_type}
                {selected.repeats ? " · can match more than once per document" : ""}
              </span>
            </>
          ) : (
            <span className="muted">{placeholder}</span>
          )}
        </span>
        <span className="picker-caret">▾</span>
      </button>

      {open && (
        <div className="picker-menu">
          <input
            ref={searchRef}
            className="picker-search"
            placeholder={`Search ${fields.length} field${fields.length === 1 ? "" : "s"}…`}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onSearchKey}
          />
          <div className="picker-list" role="listbox">
            {filtered.map((f, i) => (
              <div
                key={f.path_pattern}
                role="option"
                aria-selected={f.path_pattern === value}
                className={
                  "picker-option" +
                  (f.path_pattern === value ? " selected" : "") +
                  (i === active ? " active" : "")
                }
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(f)}
              >
                <div className="picker-option-name">
                  {f.label}
                  {f.path_pattern === value && <span className="tick">✓</span>}
                  <span className="pill" style={{ marginLeft: "auto" }}>{f.data_type}</span>
                  {f.repeats && <span className="pill repeats">repeats</span>}
                </div>
                <div className="picker-option-desc">{f.path_pattern}</div>
              </div>
            ))}
            {filtered.length === 0 && (
              <div className="picker-empty muted">No field matches "{query}".</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
