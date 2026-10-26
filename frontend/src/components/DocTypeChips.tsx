import { useEffect, useMemo, useRef, useState } from "react";

interface ChipType { id: string; name: string; value_count?: number }
interface AllType { id: string; name: string; description?: string; is_enabled?: boolean }

interface Props {
  types: ChipType[];
  allTypes: AllType[];
  hintFor: (name: string) => string | undefined;
  canEdit: boolean;
  busyId: string | null;          // a type being added or removed
  onAdd: (typeId: string) => void;
  onRemove: (typeId: string) => void;
}

/** The document's types as chips: × removes one (after a warning), "+ Add type"
 * opens a searchable list of the types it doesn't have. Both live where the types
 * are shown, so correcting a wrong classification is one click from the label. */
export function DocTypeChips({ types, allTypes, hintFor, canEdit, busyId, onAdd, onRemove }: Props) {
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const [removing, setRemoving] = useState<ChipType | null>(null);
  const rootRef = useRef<HTMLSpanElement>(null);

  const available = useMemo(() => {
    const have = new Set(types.map((t) => t.id));
    const q = query.trim().toLowerCase();
    return allTypes
      .filter((t) => t.is_enabled !== false && !have.has(t.id))
      .filter((t) => !q || t.name.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q));
  }, [types, allTypes, query]);

  useEffect(() => {
    if (!adding) return;
    const down = (e: MouseEvent) => { if (!rootRef.current?.contains(e.target as Node)) setAdding(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setAdding(false); };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", down); document.removeEventListener("keydown", key); };
  }, [adding]);

  return (
    <>
      {types.map((t) => (
        <span key={t.id} className="pill type-chip" title={hintFor(t.name)}>
          {t.name}
          {canEdit && (
            <button className="tag-x" disabled={busyId !== null} title={`Remove ${t.name}`}
                    aria-label={`Remove ${t.name}`} onClick={() => setRemoving(t)}>
              {busyId === t.id ? "…" : "×"}
            </button>
          )}
        </span>
      ))}
      {canEdit && (
        <span className="type-add" ref={rootRef}>
          <button className="ghost type-add-btn" disabled={busyId !== null}
                  onClick={() => { setQuery(""); setAdding((v) => !v); }}>
            {busyId && !types.some((t) => t.id === busyId) ? "Reading…" : "+ Add type"}
          </button>
          {adding && (
            <div className="type-add-pop">
              <input autoFocus placeholder="Find a document type…" value={query}
                     onChange={(e) => setQuery(e.target.value)} />
              <div className="type-add-list">
                {available.length === 0 && <div className="muted type-add-empty">No other document types.</div>}
                {available.map((t) => (
                  <button key={t.id} className="type-add-item" onClick={() => { setAdding(false); onAdd(t.id); }}>
                    <strong>{t.name}</strong>
                    {t.description && <span className="muted">{t.description}</span>}
                  </button>
                ))}
              </div>
              <div className="muted type-add-note">Its fields are read from this document right away.</div>
            </div>
          )}
        </span>
      )}

      {removing && (
        <div className="modal-overlay" onClick={() => setRemoving(null)}>
          <div className="modal-card" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginTop: 0 }}>Remove “{removing.name}”?</h3>
            <p>
              This document will no longer be a {removing.name}. The data extracted for it
              {removing.value_count ? ` (${removing.value_count} value${removing.value_count === 1 ? "" : "s"})` : ""}
              {" "}— including any suggested new fields — will be <strong>deleted</strong>, along with anything
              you reviewed or corrected there. Other types are not affected.
            </p>
            <p className="muted" style={{ fontSize: 13 }}>
              Reprocessing keeps your choice. You can add the type back later and its fields will be read again.
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button className="ghost" onClick={() => setRemoving(null)}>Cancel</button>
              <button className="ghost danger" onClick={() => { const t = removing; setRemoving(null); onRemove(t.id); }}>
                Remove and delete data
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
