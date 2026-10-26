import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { FieldNode } from "./FieldTree";

type Shape = "value" | "list" | "items" | "group";

interface Column {
  name: string;
  data_type: string;
}

interface Suggestion {
  id: string;
  name: string;
  description: string;
  data_type: string;
  shape?: Shape;
  columns?: Column[];
  document_type_id: string;
  existing_key: string | null;
}

interface TypeRef {
  id: string;
  name: string;
  fields: { key: string; name: string; data_type: string }[];
}

interface Row {
  node: FieldNode;
  include: boolean;
  name: string;
  description: string;
  data_type: string; // a single value's type, or the type of each entry of a list
  shape: Shape;
  columns: Column[]; // for "items" and "group"
  document_type_id: string;
  existing_key: string; // "" = add as a new field
}

export interface PromoteResult {
  field_value_id: string;
  document_type_name: string;
  name: string;
  created_field: boolean;
  schema_version: number | null;
  shape?: Shape;
  items?: number;
}

const DATA_TYPES: { value: string; label: string }[] = [
  { value: "string", label: "Text" },
  { value: "number", label: "Number" },
  { value: "integer", label: "Whole number" },
  { value: "currency", label: "Currency" },
  { value: "date", label: "Date" },
  { value: "datetime", label: "Date & time" },
  { value: "time", label: "Time" },
  { value: "boolean", label: "Yes / No" },
];
const PLAIN = new Set(DATA_TYPES.map((d) => d.value));

const SHAPES: { value: Shape; label: string; hint: string }[] = [
  { value: "value", label: "Single value", hint: "One fact: a date, an amount, a name, a clause." },
  { value: "list", label: "List of values", hint: "Several short values of one kind, e.g. languages." },
  { value: "items", label: "List of items (table)", hint: "Several entries, each with columns, e.g. an item and its quantity." },
  { value: "group", label: "Group of fields", hint: "One record made of named parts, e.g. an address." },
];

const DEFAULT_COLUMNS: Record<"items" | "group", Column[]> = {
  items: [{ name: "Item", data_type: "string" }, { name: "Quantity", data_type: "integer" }],
  group: [{ name: "Name", data_type: "string" }],
};

interface Props {
  docId: string;
  nodes: FieldNode[];
  /** The document's own types: the only valid homes for a new field. */
  docTypes: { id: string; name: string }[];
  /** Shown as a second way out, e.g. "Accept the rest, leave these as new". */
  alternative?: { label: string; onClick: () => void };
  onClose: () => void;
  onDone: (results: PromoteResult[]) => void;
}

/** Adds the document's new fields to a document type. Everything is pre-filled
 * (by the AI where available) and editable; nothing is saved until the
 * administrator confirms. A field that isn't a single value (a list of items,
 * say) is read from the document again for just that field, since the value
 * found so far is one string. */
export function PromoteDialog({ docId, nodes, docTypes, alternative, onClose, onDone }: Props) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [types, setTypes] = useState<TypeRef[]>([]);
  const [usedAi, setUsedAi] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const nodeKey = nodes.map((n) => n.id).join(",");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  // Ask for suggestions once. If that fails, start from the AI's own labels so
  // the form is still usable -- suggestions are a convenience, not a gate.
  useEffect(() => {
    let live = true;
    const home = (n: FieldNode) => n.document_type_id || docTypes[0]?.id || "";
    api
      .post(`/api/documents/${docId}/fields/suggest`, { field_value_ids: nodes.map((n) => n.id) })
      .then((res) => {
        if (!live) return;
        const byId = new Map<string, Suggestion>(res.suggestions.map((s: Suggestion) => [s.id, s]));
        setTypes(res.document_types);
        setUsedAi(!!res.ai);
        setRows(nodes.map((n) => {
          const s = byId.get(n.id);
          const shape: Shape = s?.shape ?? "value";
          return {
            node: n, include: true,
            name: s?.name ?? n.field_name,
            description: s?.description ?? "",
            data_type: s && PLAIN.has(s.data_type) ? s.data_type : "string",
            shape,
            columns: (s?.columns && s.columns.length
              ? s.columns
              : shape === "items" || shape === "group" ? DEFAULT_COLUMNS[shape] : []),
            document_type_id: s?.document_type_id ?? home(n),
            existing_key: s?.existing_key ?? "",
          };
        }));
      })
      .catch(() => {
        if (!live) return;
        setTypes(docTypes.map((t) => ({ ...t, fields: [] })));
        setUsedAi(false);
        setRows(nodes.map((n) => ({
          node: n, include: true, name: n.field_name, description: "", data_type: "string",
          shape: "value" as Shape, columns: [], document_type_id: home(n), existing_key: "",
        })));
      });
    return () => { live = false; };
    // Keyed on the ids, not the array: a new array each render must not refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, nodeKey]);

  const typeById = useMemo(() => new Map(types.map((t) => [t.id, t])), [types]);
  const plainFields = (typeId: string) =>
    (typeById.get(typeId)?.fields || []).filter((f) => PLAIN.has(f.data_type));

  function patch(i: number, p: Partial<Row>) {
    setRows((rs) => rs && rs.map((r, j) => (j === i ? { ...r, ...p } : r)));
  }

  /** A different home type has different existing fields, so any mapping chosen
   * for the old one no longer applies. */
  function changeType(i: number, typeId: string) {
    patch(i, { document_type_id: typeId, existing_key: "" });
  }

  /** Switching shape keeps what makes sense: a table or group needs columns (seeded
   * if there are none), and only a single value can be mapped onto an existing field. */
  function changeShape(i: number, shape: Shape) {
    setRows((rs) => rs && rs.map((r, j) => {
      if (j !== i) return r;
      const needsCols = shape === "items" || shape === "group";
      return {
        ...r, shape,
        columns: needsCols && !r.columns.length ? DEFAULT_COLUMNS[shape as "items" | "group"] : r.columns,
        existing_key: shape === "value" ? r.existing_key : "",
      };
    }));
  }

  const patchCol = (i: number, ci: number, p: Partial<Column>) =>
    setRows((rs) => rs && rs.map((r, j) => (j !== i ? r : {
      ...r, columns: r.columns.map((c, k) => (k === ci ? { ...c, ...p } : c)),
    })));
  const addCol = (i: number) =>
    setRows((rs) => rs && rs.map((r, j) => (j !== i || r.columns.length >= 12 ? r : {
      ...r, columns: [...r.columns, { name: "", data_type: "string" }],
    })));
  const removeCol = (i: number, ci: number) =>
    setRows((rs) => rs && rs.map((r, j) => (j !== i ? r : { ...r, columns: r.columns.filter((_, k) => k !== ci) })));

  const chosen = (rows || []).filter((r) => r.include);
  const structured = chosen.some((r) => r.shape !== "value");

  async function submit() {
    if (!chosen.length) return;
    if (chosen.some((r) => !r.existing_key && !r.name.trim())) {
      setError("Give every new field a name.");
      return;
    }
    const bad = chosen.find((r) => (r.shape === "items" || r.shape === "group")
      && !r.columns.some((c) => c.name.trim()));
    if (bad) {
      setError(`“${bad.name || bad.node.field_name}” needs at least one named column.`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await api.post(`/api/documents/${docId}/fields/promote`, {
        items: chosen.map((r) => ({
          field_value_id: r.node.id,
          name: r.name,
          description: r.description,
          data_type: r.data_type,
          document_type_id: r.document_type_id,
          existing_key: r.existing_key || null,
          shape: r.shape,
          columns: r.shape === "items" || r.shape === "group"
            ? r.columns.filter((c) => c.name.trim()).map((c) => ({ name: c.name.trim(), data_type: c.data_type }))
            : null,
        })),
      });
      onDone(res.promoted);
    } catch (e: any) {
      setError(e.message || String(e));
      setBusy(false);
    }
  }

  const count = chosen.length;
  const created = chosen.filter((r) => !r.existing_key).length;

  return (
    <div className="modal-overlay" onClick={() => !busy && onClose()}>
      <div className="modal-card promote-card" onClick={(e) => e.stopPropagation()}
           role="dialog" aria-label="Add new fields to a document type">
        <div className="spread" style={{ marginBottom: 6 }}>
          <h2 style={{ margin: 0 }}>Add {nodes.length === 1 ? "field" : "new fields"} to a document type</h2>
          <button className="ghost" onClick={onClose} disabled={busy}>✕ Close</button>
        </div>
        <div className="muted" style={{ fontSize: 13, marginBottom: 14 }}>
          These facts were found by the AI but no document type defines them. Adding one makes it a regular
          field that every document of that type is read for, and accepts its value here.
          {rows && !usedAi && " (Suggestions are basic right now — please review each one.)"}
        </div>

        {!rows && <div className="muted promote-loading">Looking at the document to suggest names, descriptions and types…</div>}

        {rows && (
          <div className="promote-rows">
            {rows.map((r, i) => {
              const existing = plainFields(r.document_type_id);
              const mapped = r.existing_key ? existing.find((f) => f.key === r.existing_key) : undefined;
              const shape = SHAPES.find((s) => s.value === r.shape)!;
              return (
                <div key={r.node.id} className={`promote-row${r.include ? "" : " off"}`}>
                  <div className="promote-src">
                    <label className="promote-check">
                      <input type="checkbox" checked={r.include} disabled={busy}
                             onChange={(e) => patch(i, { include: e.target.checked })} />
                      <span>
                        <strong>{r.node.field_name}</strong>
                        <span className="muted"> · AI found</span>
                      </span>
                    </label>
                    <div className="promote-value" title={r.node.raw_value || ""}>{r.node.raw_value || "—"}</div>
                  </div>

                  {r.include && (
                    <div className="promote-form">
                      {existing.length > 0 && r.shape === "value" && (
                        <div className="field">
                          <label>Treat as</label>
                          <select value={r.existing_key} disabled={busy}
                                  onChange={(e) => patch(i, { existing_key: e.target.value })}>
                            <option value="">A new field</option>
                            {existing.map((f) => (
                              <option key={f.key} value={f.key}>Same as existing “{f.name}”</option>
                            ))}
                          </select>
                        </div>
                      )}

                      {mapped ? (
                        <div className="muted" style={{ fontSize: 12 }}>
                          Fits the existing field “{mapped.name}”, so no new field is created — this
                          document's value just moves there.
                        </div>
                      ) : (
                        <>
                          <div className="promote-grid">
                            <div className="field">
                              <label>Field name</label>
                              <input value={r.name} disabled={busy}
                                     onChange={(e) => patch(i, { name: e.target.value })} />
                            </div>
                            <div className="field">
                              <label>Shape</label>
                              <select value={r.shape} disabled={busy}
                                      onChange={(e) => changeShape(i, e.target.value as Shape)}>
                                {SHAPES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                              </select>
                            </div>
                          </div>
                          <div className="muted" style={{ fontSize: 12, margin: "-6px 0 10px" }}>{shape.hint}</div>

                          {(r.shape === "value" || r.shape === "list") && (
                            <div className="field" style={{ maxWidth: 220 }}>
                              <label>{r.shape === "list" ? "Type of each value" : "Data type"}</label>
                              <select value={r.data_type} disabled={busy}
                                      onChange={(e) => patch(i, { data_type: e.target.value })}>
                                {DATA_TYPES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
                              </select>
                            </div>
                          )}

                          {(r.shape === "items" || r.shape === "group") && (
                            <div className="field">
                              <label>
                                Columns{" "}
                                <span className="muted">
                                  ({r.shape === "items" ? "each item becomes a row with these" : "the parts of the record"})
                                </span>
                              </label>
                              {r.columns.map((c, ci) => (
                                <div key={ci} className="col-row">
                                  <input placeholder="Column name" value={c.name} disabled={busy}
                                         onChange={(e) => patchCol(i, ci, { name: e.target.value })} />
                                  <select value={c.data_type} disabled={busy}
                                          onChange={(e) => patchCol(i, ci, { data_type: e.target.value })}>
                                    {DATA_TYPES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
                                  </select>
                                  <button type="button" className="ghost reject tiny" disabled={busy || r.columns.length <= 1}
                                          title="Remove column" onClick={() => removeCol(i, ci)}>✕</button>
                                </div>
                              ))}
                              <button type="button" className="secondary tiny" disabled={busy || r.columns.length >= 12}
                                      onClick={() => addCol(i)}>+ Add column</button>
                            </div>
                          )}

                          <div className="field">
                            <label>Description <span className="muted">(tells the AI what to look for)</span></label>
                            <textarea rows={2} value={r.description} disabled={busy}
                                      onChange={(e) => patch(i, { description: e.target.value })} />
                          </div>

                          {r.shape !== "value" && (
                            <div className="muted" style={{ fontSize: 12 }}>
                              The document will be read again for just this field, so it can be split into{" "}
                              {r.shape === "group" ? "its parts" : "separate entries"}. Your other values aren't touched.
                            </div>
                          )}
                        </>
                      )}

                      {docTypes.length > 1 && (
                        <div className="field">
                          <label>Add to document type</label>
                          <select value={r.document_type_id} disabled={busy}
                                  onChange={(e) => changeType(i, e.target.value)}>
                            {docTypes.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                          </select>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {error && <div className="err" style={{ marginTop: 12 }}>{error}</div>}

        <div className="promote-foot">
          <div className="muted" style={{ fontSize: 12 }}>
            {rows && count > 0 && (
              <>
                {created > 0 && `${created} new field${created === 1 ? "" : "s"} will be added`}
                {created > 0 && created < count && " · "}
                {created < count && `${count - created} mapped to existing`}
                . Other documents of the type can read them with “Extract new fields”.
              </>
            )}
            {rows && count === 0 && "Tick at least one field."}
          </div>
          <div className="row">
            {alternative && (
              <button className="ghost" disabled={busy} onClick={alternative.onClick}>{alternative.label}</button>
            )}
            <button className="ghost" onClick={onClose} disabled={busy}>Cancel</button>
            <button onClick={submit} disabled={busy || !rows || count === 0}>
              {busy
                ? structured ? "Reading the document…" : "Adding…"
                : count > 1 ? `Add ${count} fields & accept` : structured ? "Add field & read it" : "Add field & accept"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
