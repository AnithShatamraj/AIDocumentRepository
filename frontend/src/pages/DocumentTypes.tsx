import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import {
  FieldDefDraft,
  SchemaBuilder,
  emptyField,
  fromSchema,
  toPayload,
} from "../components/SchemaBuilder";

/** Compact preview of a field tree, so the list shows shape at a glance. */
function SchemaOutline({ fields, depth = 0 }: { fields: any[]; depth?: number }) {
  return (
    <>
      {(fields || []).map((f, i) => (
        <div key={i} style={{ marginLeft: depth * 12, fontSize: 12 }} className="muted">
          <span style={{ color: "var(--text)" }}>{f.name}</span>{" "}
          <span className="pill">
            {f.data_type === "list"
              ? `list of ${f.item?.data_type === "object" ? "objects" : f.item?.data_type || "?"}`
              : f.data_type}
          </span>
          {f.data_type === "object" && <SchemaOutline fields={f.fields} depth={depth + 1} />}
          {f.data_type === "list" && f.item?.data_type === "object" && (
            <SchemaOutline fields={f.item.fields} depth={depth + 1} />
          )}
        </div>
      ))}
    </>
  );
}

const STAGE_LABEL: Record<string, string> = {
  describe: "Describing", name: "Naming", samples: "Samples", fields: "Designing fields",
};

export function DocumentTypes() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const location = useLocation();
  const publishedName: string | undefined = (location.state as any)?.published;
  const [editing, setEditing] = useState<any | null>(null); // null = closed; an existing type otherwise
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [fields, setFields] = useState<FieldDefDraft[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const { data: types } = useQuery({
    queryKey: ["document-types"],
    queryFn: () => api.get("/api/document-types"),
  });

  const { data: drafts } = useQuery({
    queryKey: ["type-drafts"],
    queryFn: () => api.get("/api/document-type-drafts"),
  });

  // Details (schema + version) for every type, for the outline column.
  const { data: details } = useQuery({
    queryKey: ["document-type-details", (types || []).map((t: any) => t.id).join(",")],
    enabled: !!types?.length,
    queryFn: async () =>
      Object.fromEntries(
        await Promise.all(
          (types || []).map(async (t: any) => [t.id, await api.get(`/api/document-types/${t.id}`)])
        )
      ),
  });

  // New types are authored in the AI-assisted builder page, not in this modal.
  async function startNew() {
    setErr("");
    setBusy(true);
    try {
      const d = await api.post("/api/document-type-drafts");
      qc.invalidateQueries({ queryKey: ["type-drafts"] });
      nav(`/document-types/drafts/${d.id}`);
    } catch (e: any) {
      setErr(e.message || String(e));
      setBusy(false);
    }
  }

  async function discardDraft(d: any) {
    if (!window.confirm(`Discard the draft “${d.title}”? Its conversation and samples are deleted.`)) return;
    try {
      await api.del(`/api/document-type-drafts/${d.id}`);
      qc.invalidateQueries({ queryKey: ["type-drafts"] });
    } catch (e: any) {
      setErr(e.message || String(e));
    }
  }

  async function startEdit(t: any) {
    setErr("");
    const full = await api.get(`/api/document-types/${t.id}`);
    setEditing(full);
    setName(full.name);
    setDesc(full.description || "");
    setFields(full.fields?.length ? fromSchema(full.fields) : [emptyField()]);
  }

  async function save() {
    setErr("");
    setBusy(true);
    try {
      const payload = toPayload(fields);
      if (!name.trim()) throw new Error("Give the document type a name.");
      if (!payload.length) throw new Error("Add at least one field.");

      // Server-side validation first — same rules the extractor will enforce.
      const check = await api.post("/api/document-types/validate", { name, fields: payload });
      if (!check.valid) throw new Error(check.error);

      await api.patch(`/api/document-types/${editing.id}`, {
        name, description: desc, fields: payload,
      });
      qc.invalidateQueries({ queryKey: ["document-types"] });
      qc.invalidateQueries({ queryKey: ["document-type-details"] });
      setEditing(null);
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(t: any) {
    await api.patch(`/api/document-types/${t.id}`, { is_enabled: !t.is_enabled });
    qc.invalidateQueries({ queryKey: ["document-types"] });
  }

  async function remove(t: any) {
    setErr("");
    try {
      await api.del(`/api/document-types/${t.id}`);
      qc.invalidateQueries({ queryKey: ["document-types"] });
    } catch (e: any) {
      setErr(e.message);
    }
  }

  // ESC closes the editor.
  useEffect(() => {
    if (editing === null) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setEditing(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing]);

  return (
    <div>
      <div className="page-head spread">
        <div>
          <h1>Document Types</h1>
          <div className="muted">
            Each type defines the fields the AI extracts. Fields can be grouped (objects) or
            repeat (lists) — editing them creates a new schema version.
          </div>
        </div>
        <button onClick={startNew} disabled={busy}>+ New document type</button>
      </div>

      {publishedName && (
        <div className="card ok" style={{ marginBottom: 14 }}>
          Published <strong>{publishedName}</strong> — it's now available when uploading documents.
        </div>
      )}
      {err && !editing && <div className="card err" style={{ marginBottom: 14 }}>{err}</div>}

      {drafts && drafts.length > 0 && (
        <div style={{ marginBottom: 18 }}>
          <div className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 8 }}>
            In-progress drafts
          </div>
          <div className="grid cols-2">
            {drafts.map((d: any) => (
              <div className="card spread" key={d.id}>
                <div style={{ minWidth: 0 }}>
                  <strong style={{ fontSize: 14 }}>{d.title}</strong>{" "}
                  <span className="pill">{STAGE_LABEL[d.stage] || d.stage}</span>
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                    {d.field_count} field(s) · {d.sample_count} sample(s) · edited{" "}
                    {new Date(d.updated_at).toLocaleString()}
                  </div>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  <button className="secondary" onClick={() => nav(`/document-types/drafts/${d.id}`)}>Resume</button>
                  <button className="ghost reject" onClick={() => discardDraft(d)} title="Discard draft">✕</button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="grid cols-2">
        {(types || []).map((t: any) => {
          const d = details?.[t.id];
          return (
            <div className="card" key={t.id}>
              <div className="spread">
                <div>
                  <strong style={{ fontSize: 15 }}>{t.name}</strong>{" "}
                  {t.is_system && <span className="pill">built-in</span>}{" "}
                  {!t.is_enabled && <span className="pill" style={{ color: "var(--muted)" }}>disabled</span>}
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{t.description}</div>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  <button className="secondary" onClick={() => startEdit(t)}>Edit fields</button>
                  <button className="ghost" onClick={() => toggle(t)}>
                    {t.is_enabled ? "Disable" : "Enable"}
                  </button>
                  <button className="ghost reject" onClick={() => remove(t)} title="Delete type">✕</button>
                </div>
              </div>
              <div style={{ marginTop: 10, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
                <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                  Schema v{d?.schema_version ?? "—"} · {d?.fields?.length ?? 0} top-level field(s)
                </div>
                {d ? <SchemaOutline fields={d.fields} /> : <span className="muted">Loading…</span>}
              </div>
            </div>
          );
        })}
        {types && types.length === 0 && (
          <div className="card muted">No document types yet — create one to tell the AI what to extract.</div>
        )}
      </div>

      {editing !== null && (
        <div className="modal-overlay" onClick={() => !busy && setEditing(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="spread" style={{ marginBottom: 16 }}>
              <h2 style={{ margin: 0 }}>
                Edit “{editing.name}”
                <span className="pill" style={{ marginLeft: 8 }}>saves as v{(editing.schema_version || 0) + 1}</span>
              </h2>
              <button className="ghost" onClick={() => setEditing(null)} disabled={busy}>✕ Close</button>
            </div>

            <div className="grid cols-2" style={{ marginBottom: 8 }}>
              <div className="field">
                <label>Name</label>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Purchase Order" />
              </div>
              <div className="field">
                <label>Description</label>
                <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="What kind of document is this?" />
              </div>
            </div>

            <div className="spread" style={{ margin: "6px 0 10px" }}>
              <strong>Fields</strong>
              <span className="muted" style={{ fontSize: 12 }}>
                Use <em>object</em> to group related fields, <em>list</em> for things that repeat
                (parties, line items, work experience).
              </span>
            </div>

            <SchemaBuilder nodes={fields} onChange={setFields} />

            <button className="secondary" onClick={() => setFields([...fields, emptyField()])}>
              + Add field
            </button>

            {err && <div className="err" style={{ marginTop: 12 }}>{err}</div>}

            <div className="row" style={{ marginTop: 18, borderTop: "1px solid var(--border)", paddingTop: 14 }}>
              <button onClick={save} disabled={busy}>
                {busy ? "Saving…" : "Save new version"}
              </button>
              <button className="ghost" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
