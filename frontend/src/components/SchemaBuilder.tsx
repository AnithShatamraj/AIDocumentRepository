import { useState } from "react";

export type DataType =
  | "string" | "number" | "integer" | "boolean"
  | "date" | "time" | "datetime" | "currency"
  | "object" | "list";

export interface FieldDefDraft {
  uid: string; // client-side identity only; the server derives `key` from `name`
  key?: string;
  name: string;
  description: string;
  data_type: DataType;
  required?: boolean;
  fields?: FieldDefDraft[]; // object children
  item?: FieldDefDraft | null; // list element
}

const SCALARS: DataType[] = [
  "string", "number", "integer", "boolean", "date", "time", "datetime", "currency",
];
const ALL: DataType[] = [...SCALARS, "object", "list"];
const MAX_DEPTH = 5;

export const uid = () => Math.random().toString(36).slice(2, 9);

export function emptyField(data_type: DataType = "string"): FieldDefDraft {
  return { uid: uid(), name: "", description: "", data_type, fields: [], item: null };
}

/** Strip client-side ids and empty branches before sending to the API. */
export function toPayload(nodes: FieldDefDraft[]): any[] {
  return nodes
    .filter((n) => n.name.trim())
    .map((n) => {
      const out: any = {
        name: n.name.trim(),
        description: n.description.trim(),
        data_type: n.data_type,
        required: !!n.required,
      };
      if (n.key) out.key = n.key;
      if (n.data_type === "object") out.fields = toPayload(n.fields || []);
      if (n.data_type === "list") {
        const item = n.item || emptyField("string");
        out.item = {
          name: item.name.trim() || `${n.name.trim()} item`,
          description: item.description.trim(),
          data_type: item.data_type,
          ...(item.data_type === "object" ? { fields: toPayload(item.fields || []) } : {}),
        };
      }
      return out;
    });
}

/** Server shape -> editable drafts (for editing an existing type). */
export function fromSchema(nodes: any[]): FieldDefDraft[] {
  return (nodes || []).map((n) => ({
    uid: uid(),
    key: n.key,
    name: n.name || "",
    description: n.description || "",
    data_type: (n.data_type || "string") as DataType,
    required: !!n.required,
    fields: n.fields ? fromSchema(n.fields) : [],
    item: n.item
      ? {
          uid: uid(),
          key: n.item.key,
          name: n.item.name || "",
          description: n.item.description || "",
          data_type: (n.item.data_type || "string") as DataType,
          fields: n.item.fields ? fromSchema(n.item.fields) : [],
          item: null,
        }
      : null,
  }));
}

interface Props {
  nodes: FieldDefDraft[];
  onChange: (next: FieldDefDraft[]) => void;
  depth?: number;
}

export function SchemaBuilder({ nodes, onChange, depth = 0 }: Props) {
  function update(i: number, patch: Partial<FieldDefDraft>) {
    const next = [...nodes];
    next[i] = { ...next[i], ...patch };
    onChange(next);
  }
  function remove(i: number) {
    onChange(nodes.filter((_, j) => j !== i));
  }
  function move(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= nodes.length) return;
    const next = [...nodes];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  }

  return (
    <div>
      {nodes.map((n, i) => (
        <div className="fb-node" key={n.uid}>
          <div className="fb-row">
            <div style={{ flex: "2 1 180px" }}>
              <div className="fb-label">Field name</div>
              <input
                value={n.name}
                placeholder="e.g. Invoice Number"
                onChange={(e) => update(i, { name: e.target.value })}
              />
            </div>
            <div style={{ flex: "1 1 130px" }}>
              <div className="fb-label">Data type</div>
              <select
                value={n.data_type}
                onChange={(e) => {
                  const dt = e.target.value as DataType;
                  update(i, {
                    data_type: dt,
                    fields: dt === "object" ? (n.fields?.length ? n.fields : [emptyField()]) : n.fields,
                    item: dt === "list" ? n.item || emptyField("string") : null,
                  });
                }}
              >
                {ALL.map((t) => (
                  <option key={t} value={t}>
                    {t === "object" ? "object (group)" : t === "list" ? "list (repeating)" : t}
                  </option>
                ))}
              </select>
            </div>
            <div style={{ flex: "3 1 240px" }}>
              <div className="fb-label">Description — this is the instruction given to the AI</div>
              <input
                value={n.description}
                placeholder="e.g. Unique identifier printed on the invoice"
                onChange={(e) => update(i, { description: e.target.value })}
              />
            </div>
            <div className="row" style={{ gap: 4, alignItems: "flex-end", paddingBottom: 1 }}>
              <button className="ghost" title="Move up" onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
              <button className="ghost" title="Move down" onClick={() => move(i, 1)} disabled={i === nodes.length - 1}>↓</button>
              <button className="ghost reject" title="Remove field" onClick={() => remove(i)}>✕</button>
            </div>
          </div>

          {/* object -> nested children */}
          {n.data_type === "object" && (
            <div className="fb-children">
              <div className="fb-label" style={{ marginBottom: 6 }}>Fields inside this group</div>
              {depth + 1 >= MAX_DEPTH ? (
                <div className="err" style={{ fontSize: 12 }}>Maximum nesting depth reached.</div>
              ) : (
                <>
                  <SchemaBuilder
                    nodes={n.fields || []}
                    depth={depth + 1}
                    onChange={(kids) => update(i, { fields: kids })}
                  />
                  <button className="secondary" onClick={() => update(i, { fields: [...(n.fields || []), emptyField()] })}>
                    + Add field
                  </button>
                </>
              )}
            </div>
          )}

          {/* list -> element definition */}
          {n.data_type === "list" && (
            <div className="fb-children">
              <div className="fb-row" style={{ alignItems: "flex-end" }}>
                <div style={{ flex: "1 1 160px" }}>
                  <div className="fb-label">Each item is a…</div>
                  <select
                    value={n.item?.data_type || "string"}
                    onChange={(e) => {
                      const dt = e.target.value as DataType;
                      update(i, {
                        item: {
                          ...(n.item || emptyField()),
                          data_type: dt,
                          fields: dt === "object" ? (n.item?.fields?.length ? n.item.fields : [emptyField()]) : [],
                        },
                      });
                    }}
                  >
                    {ALL.filter((t) => t !== "list").map((t) => (
                      <option key={t} value={t}>{t === "object" ? "object (group of fields)" : t}</option>
                    ))}
                  </select>
                </div>
                <div style={{ flex: "2 1 200px" }}>
                  <div className="fb-label">Item label (optional)</div>
                  <input
                    value={n.item?.name || ""}
                    placeholder="e.g. Line Item"
                    onChange={(e) => update(i, { item: { ...(n.item || emptyField()), name: e.target.value } })}
                  />
                </div>
              </div>

              {n.item?.data_type === "object" && (
                depth + 2 >= MAX_DEPTH ? (
                  <div className="err" style={{ fontSize: 12, marginTop: 8 }}>Maximum nesting depth reached.</div>
                ) : (
                  <div style={{ marginTop: 8 }}>
                    <div className="fb-label" style={{ marginBottom: 6 }}>Fields on each item</div>
                    <SchemaBuilder
                      nodes={n.item.fields || []}
                      depth={depth + 2}
                      onChange={(kids) => update(i, { item: { ...(n.item as FieldDefDraft), fields: kids } })}
                    />
                    <button
                      className="secondary"
                      onClick={() =>
                        update(i, {
                          item: { ...(n.item as FieldDefDraft), fields: [...(n.item?.fields || []), emptyField()] },
                        })
                      }
                    >
                      + Add field to item
                    </button>
                  </div>
                )
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
