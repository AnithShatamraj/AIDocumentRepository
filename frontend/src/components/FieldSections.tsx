import { useMemo, useState } from "react";
import { FieldNode, FieldTree } from "./FieldTree";

interface Props {
  fields: FieldNode[];
  docTypes: { id: string; name: string }[];
  isPdf: boolean;
  reviewing: Record<string, boolean>;
  onJump: (n: FieldNode) => void;
  onReview: (id: string, action: "accept" | "reject") => void;
  onEdit: (id: string, value: string) => void;
  onDelete: (id: string) => void;
  /** Administrators can add new fields to a document type; others only see them. */
  canPromote: boolean;
  /** Open the add-to-type dialog for these new fields. */
  onPromote: (nodes: FieldNode[]) => void;
}

// Each document type gets its own hue (cycling), so one type is told from the next
// at a glance; "New fields" and "Other fields" have fixed ones. See index.css.
const TYPE_HUES = 4;
const NEW_ID = "__new__";
const OTHER_ID = "__other__";

const pendingIn = (nodes: FieldNode[]): number =>
  nodes.reduce(
    (n, x) => n + (x.review_status === "pending_review" && x.node_kind === "scalar" ? 1 : 0) + pendingIn(x.children),
    0
  );

/** The same fact is often discovered once per document type. Show it once: the
 * most confident copy, preferring one that hasn't been rejected. */
function dedupeNew(nodes: FieldNode[]): FieldNode[] {
  const best = new Map<string, FieldNode>();
  for (const n of nodes) {
    const cur = best.get(n.field_key);
    const better =
      !cur ||
      (cur.review_status === "rejected" && n.review_status !== "rejected") ||
      (cur.review_status === n.review_status && n.confidence > cur.confidence);
    if (better) best.set(n.field_key, n);
  }
  return [...best.values()].sort((a, b) => (a.ordinal ?? 0) - (b.ordinal ?? 0) || a.field_name.localeCompare(b.field_name));
}

/** Extracted data grouped the way people think about it: one collapsible section
 * per document type, then the facts no type defines yet ("New fields"), which
 * can be promoted into a type. */
export function FieldSections({
  fields, docTypes, isPdf, reviewing, onJump, onReview, onEdit, onDelete, canPromote, onPromote,
}: Props) {
  const [closed, setClosed] = useState<Record<string, boolean>>({});

  const sections = useMemo(() => {
    const schemaNodes = fields.filter((f) => !f.is_discovered);
    const known = new Set(docTypes.map((t) => t.id));
    const out: { id: string; title: string; nodes: FieldNode[]; hue: string; isNew?: boolean; empty?: string }[] = docTypes.map((t, i) => ({
      id: t.id,
      title: t.name,
      hue: String(i % TYPE_HUES),
      nodes: schemaNodes.filter((f) => f.document_type_id === t.id),
      empty: "Nothing extracted for this type yet.",
    }));
    const other = schemaNodes.filter((f) => !f.document_type_id || !known.has(f.document_type_id));
    if (other.length) out.push({ id: OTHER_ID, title: "Other fields", nodes: other, hue: "other" });
    const fresh = dedupeNew(fields.filter((f) => f.is_discovered));
    if (fresh.length) out.push({ id: NEW_ID, title: "New fields", nodes: fresh, isNew: true, hue: "new" });
    return out;
  }, [fields, docTypes]);

  const toggle = (id: string) => setClosed((c) => ({ ...c, [id]: !c[id] }));
  const allClosed = sections.length > 0 && sections.every((s) => closed[s.id]);
  const setAll = (shut: boolean) => setClosed(Object.fromEntries(sections.map((s) => [s.id, shut])));

  if (!sections.length) return <div className="muted">No fields extracted yet.</div>;

  return (
    <div className="fs">
      {sections.length > 1 && (
        <div className="fs-toolbar">
          <button className="ghost tiny" onClick={() => setAll(!allClosed)}>
            {allClosed ? "Expand all" : "Collapse all"}
          </button>
        </div>
      )}

      {sections.map((s) => {
        const open = !closed[s.id];
        const pending = pendingIn(s.nodes);
        // New fields that can still be added (a rejected one has to be restored first).
        const addable = s.isNew ? s.nodes.filter((n) => n.review_status !== "rejected") : [];
        return (
          <div key={s.id} className={`fs-section${s.isNew ? " is-new" : ""}`} data-hue={s.hue}>
            <div className="fs-head" onClick={() => toggle(s.id)} title={open ? "Collapse" : "Expand"}>
              <span className="chev">{open ? "▼" : "▶"}</span>
              <strong>{s.title}</strong>
              <span className="pill">{s.nodes.length}</span>
              {pending > 0 && <span className="pill pending">{pending} pending</span>}
              {s.isNew && addable.length > 0 && (
                <span className="fs-actions" onClick={(e) => e.stopPropagation()}>
                  <button
                    className="promote-btn"
                    disabled={!canPromote}
                    title={canPromote
                      ? "Review and add these to a document type"
                      : "Only an administrator can add fields to a document type"}
                    onClick={() => onPromote(addable)}
                  >
                    ＋ Add all to types…
                  </button>
                </span>
              )}
            </div>

            {open && (
              <div className="fs-body">
                {s.isNew && (
                  <div className="fs-note muted">
                    Found by the AI, but not defined by any document type. Add each one to a type to accept it —
                    it then becomes a regular field.
                    {!canPromote && " Ask an administrator to add them; you can still reject any that are wrong."}
                  </div>
                )}
                {s.nodes.length === 0 ? (
                  <div className="muted" style={{ fontSize: 12, padding: "4px 2px 8px" }}>{s.empty}</div>
                ) : (
                  <FieldTree
                    nodes={s.nodes}
                    isPdf={isPdf}
                    reviewing={reviewing}
                    onJump={onJump}
                    onReview={onReview}
                    onEdit={onEdit}
                    onDelete={onDelete}
                    canPromote={canPromote}
                    onPromote={s.isNew ? (n) => onPromote([n]) : undefined}
                  />
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
