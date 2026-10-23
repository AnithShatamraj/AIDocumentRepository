import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

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
  const savedName: string | undefined = (location.state as any)?.saved;
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

  // The popup's full description + field tree. Fetched when a card is opened
  // rather than for every card up front -- the list only needs the summary.
  const [viewing, setViewing] = useState<any | null>(null);
  const { data: viewDetail } = useQuery({
    queryKey: ["document-type-detail", viewing?.id],
    enabled: !!viewing,
    queryFn: () => api.get(`/api/document-types/${viewing.id}`),
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

  // Editing uses the same guided wizard as creating a type, opened on the fields
  // step with this type's name, description and fields already in place. The
  // type itself isn't touched until the admin saves from there.
  async function editType(t: any) {
    setErr("");
    setBusy(true);
    try {
      const d = await api.post(`/api/document-type-drafts/from-type/${t.id}`);
      qc.invalidateQueries({ queryKey: ["type-drafts"] });
      nav(`/document-types/drafts/${d.id}`);
    } catch (e: any) {
      setErr(e.message || String(e));
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

  // ESC closes the detail popup.
  useEffect(() => {
    if (viewing === null) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setViewing(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [viewing]);

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

      {savedName && (
        <div className="card ok" style={{ marginBottom: 14 }}>
          Saved changes to <strong>{savedName}</strong>. New and reprocessed documents use the updated fields.
        </div>
      )}
      {publishedName && (
        <div className="card ok" style={{ marginBottom: 14 }}>
          Published <strong>{publishedName}</strong> — it's now available when uploading documents.
        </div>
      )}
      {err && <div className="card err" style={{ marginBottom: 14 }}>{err}</div>}

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
                  <span className="pill">{d.edits_type_id ? "Editing" : STAGE_LABEL[d.stage] || d.stage}</span>
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

      <div className="type-grid">
        {(types || []).map((t: any) => (
          <div
            className={`card type-card${t.is_enabled ? "" : " disabled"}`}
            key={t.id}
            role="button"
            tabIndex={0}
            onClick={() => setViewing(t)}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setViewing(t))}
          >
            <div className="type-card-head">
              <strong className="type-card-name" title={t.name}>{t.name}</strong>
              {t.is_system && <span className="pill">built-in</span>}
              {!t.is_enabled && <span className="pill" style={{ color: "var(--muted)" }}>disabled</span>}
            </div>
            <div className="type-card-desc muted">{t.description || "No description."}</div>
            <div className="type-card-foot">
              <span className="muted tiny-text">
                Modified {t.updated_at ? new Date(t.updated_at).toLocaleString() : "—"}
              </span>
              {/* Buttons act on the type; they must not also open the popup. */}
              <div className="row" style={{ gap: 6 }} onClick={(e) => e.stopPropagation()}
                   onKeyDown={(e) => e.stopPropagation()}>
                <button className="secondary" disabled={busy} onClick={() => editType(t)}>Edit</button>
                <button className="ghost" onClick={() => toggle(t)}>
                  {t.is_enabled ? "Disable" : "Enable"}
                </button>
                <button className="ghost reject" onClick={() => remove(t)} title="Delete type">✕</button>
              </div>
            </div>
          </div>
        ))}
        {types && types.length === 0 && (
          <div className="card muted">No document types yet — create one to tell the AI what to extract.</div>
        )}
      </div>

      {viewing && (
        <div className="modal-overlay" onClick={() => setViewing(null)}>
          <div className="modal-card sm" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={viewing.name}>
            <div className="spread" style={{ marginBottom: 12 }}>
              <h2 style={{ margin: 0 }}>
                {viewing.name}{" "}
                {viewing.is_system && <span className="pill">built-in</span>}{" "}
                {!viewing.is_enabled && <span className="pill" style={{ color: "var(--muted)" }}>disabled</span>}
              </h2>
              <button className="ghost" onClick={() => setViewing(null)}>✕ Close</button>
            </div>
            <div className="muted tiny-text" style={{ marginBottom: 12 }}>
              Modified {viewing.updated_at ? new Date(viewing.updated_at).toLocaleString() : "—"}
              {viewDetail && <> · Schema v{viewDetail.schema_version ?? "—"}</>}
            </div>
            <div className="field-label">Description</div>
            <p style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{viewing.description || "No description."}</p>
            <div className="field-label" style={{ marginTop: 16 }}>
              Fields{viewDetail ? ` (${viewDetail.fields?.length ?? 0} top-level)` : ""}
            </div>
            <div style={{ marginTop: 6 }}>
              {viewDetail
                ? (viewDetail.fields?.length ? <SchemaOutline fields={viewDetail.fields} />
                                              : <span className="muted">No fields defined.</span>)
                : <span className="muted">Loading…</span>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
