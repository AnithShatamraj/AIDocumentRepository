import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { UploadDialog } from "../components/UploadDialog";
import { TagEditor, useTagSuggestions } from "../components/TagEditor";

export function Documents() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState<string>("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const [activeTags, setActiveTags] = useState<string[]>([]);
  const [match, setMatch] = useState<"all" | "any">("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkTags, setBulkTags] = useState<string[]>([]);
  const [bulkBusy, setBulkBusy] = useState(false);

  const { data: tagFacets } = useTagSuggestions();

  const path = useMemo(() => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    activeTags.forEach((t) => params.append("tags", t));
    if (activeTags.length) params.set("match", match);
    const qs = params.toString();
    return `/api/documents${qs ? `?${qs}` : ""}`;
  }, [q, activeTags, match]);

  const { data, isLoading } = useQuery({
    queryKey: ["documents", q, activeTags, match],
    queryFn: () => api.get(path),
    refetchInterval: 5000,
  });

  function toggleTag(name: string) {
    setActiveTags((t) =>
      t.includes(name) ? t.filter((x) => x !== name) : [...t, name]
    );
  }

  function toggleRow(id: string) {
    setSelected((s) => {
      const next = new Set(s);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  const visibleIds: string[] = (data || []).map((d: any) => d.id);
  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.has(id));

  async function bulkTag(action: "add" | "remove") {
    if (!bulkTags.length || !selected.size) return;
    setBulkBusy(true);
    setMsg("");
    try {
      const res = await api.post("/api/documents/bulk-tag", {
        document_ids: [...selected],
        add: action === "add" ? bulkTags : [],
        remove: action === "remove" ? bulkTags : [],
      });
      // `skipped` is documents in the selection the user can't edit — a normal
      // outcome when a selection spans a permission boundary, so it's reported
      // rather than treated as an error.
      setMsg(
        `${action === "add" ? "Tagged" : "Untagged"} ${res.updated} document(s).` +
          (res.skipped ? ` ${res.skipped} skipped — you can't edit them.` : "")
      );
      setBulkTags([]);
      setSelected(new Set());
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["documents"] }),
        qc.invalidateQueries({ queryKey: ["tags"] }),
      ]);
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setBulkBusy(false);
    }
  }

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setMsg("");
    try {
      const form = new FormData();
      Array.from(files).forEach((f) => form.append("files", f));
      const res = await api.upload("/api/documents/bulk", form);
      const dups = res.results.filter((r: any) => r.duplicate_of).length;
      setMsg(
        `Uploaded ${res.results.filter((r: any) => r.status === "accepted").length} file(s). ` +
          `${res.results.filter((r: any) => r.status === "rejected").length} rejected.` +
          (dups ? ` ${dups} possible duplicate(s).` : "")
      );
      qc.invalidateQueries({ queryKey: ["documents"] });
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div>
      <div className="page-head spread">
        <div>
          <h1>Documents</h1>
          <div className="muted">Upload triggers the async AI pipeline automatically.</div>
        </div>
        <div className="row">
          <input
            ref={fileRef}
            type="file"
            multiple
            style={{ display: "none" }}
            onChange={(e) => onUpload(e.target.files)}
            accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.tiff"
          />
          <button onClick={() => setDialogOpen(true)}>Upload document</button>
          <button className="secondary" disabled={uploading} onClick={() => fileRef.current?.click()}
                  title="Upload several files at once with auto-detected types">
            {uploading ? "Uploading…" : "Bulk upload"}
          </button>
        </div>
      </div>

      {msg && <div className="card" style={{ marginBottom: 16 }}>{msg}</div>}

      {dialogOpen && (
        <UploadDialog
          onClose={() => setDialogOpen(false)}
          onUploaded={() => qc.invalidateQueries({ queryKey: ["documents"] })}
        />
      )}

      <div className="toolbar">
        <input placeholder="Search by name…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320 }} />
      </div>

      {(tagFacets || []).length > 0 && (
        <div className="facets">
          {(tagFacets || []).map((t) => (
            <button
              key={t.id}
              type="button"
              className={`pill tag-facet${activeTags.includes(t.name) ? " on" : ""}`}
              onClick={() => toggleTag(t.name)}
            >
              {t.name} <span className="muted">{t.document_count}</span>
            </button>
          ))}
          {activeTags.length > 1 && (
            <button type="button" className="ghost tiny"
                    title="Whether a document must carry every selected tag or just one"
                    onClick={() => setMatch((m) => (m === "all" ? "any" : "all"))}>
              match: {match}
            </button>
          )}
          {activeTags.length > 0 && (
            <button type="button" className="ghost tiny" onClick={() => setActiveTags([])}>
              Clear tags
            </button>
          )}
        </div>
      )}

      {selected.size > 0 && (
        <div className="card bulk-bar">
          <strong>{selected.size} selected</strong>
          <TagEditor value={bulkTags} onChange={setBulkTags} placeholder="Tag to apply…" />
          <button disabled={bulkBusy || !bulkTags.length} onClick={() => bulkTag("add")}>
            Add to selected
          </button>
          <button className="secondary" disabled={bulkBusy || !bulkTags.length}
                  onClick={() => bulkTag("remove")}>
            Remove from selected
          </button>
          <button className="ghost" onClick={() => setSelected(new Set())}>Clear selection</button>
        </div>
      )}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th className="pick">
                <input
                  type="checkbox"
                  checked={allSelected}
                  title={allSelected ? "Deselect all" : "Select all"}
                  onChange={() =>
                    setSelected(allSelected ? new Set() : new Set(visibleIds))
                  }
                />
              </th>
              <th>Name</th><th>Tags</th><th>Type</th><th>Status</th><th>Ver.</th><th>Uploaded</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && <tr><td className="muted">Loading…</td></tr>}
            {(data || []).map((d: any) => (
              <tr key={d.id} className={selected.has(d.id) ? "picked" : ""}>
                <td className="pick">
                  <input type="checkbox" checked={selected.has(d.id)} onChange={() => toggleRow(d.id)} />
                </td>
                <td><Link to={`/documents/${d.id}`}>{d.name}</Link></td>
                <td>
                  {(d.tags || []).map((t: any) => (
                    <span key={t.id} className="pill tag">{t.name}</span>
                  ))}
                </td>
                <td className="muted">{d.file_type?.toUpperCase()}</td>
                <td><StatusBadge status={d.processing_status} /></td>
                <td className="muted">v{d.current_version}</td>
                <td className="muted">{new Date(d.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr><td colSpan={7} className="muted">
                {activeTags.length || q ? "No documents match these filters." : "No documents yet — upload to begin."}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
