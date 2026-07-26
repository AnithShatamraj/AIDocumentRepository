import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";

export function Documents() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState<string>("");
  const fileRef = useRef<HTMLInputElement>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["documents", q],
    queryFn: () => api.get(`/api/documents${q ? `?q=${encodeURIComponent(q)}` : ""}`),
    refetchInterval: 5000,
  });

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
          <button disabled={uploading} onClick={() => fileRef.current?.click()}>
            {uploading ? "Uploading…" : "Upload documents"}
          </button>
        </div>
      </div>

      {msg && <div className="card" style={{ marginBottom: 16 }}>{msg}</div>}

      <div className="toolbar">
        <input placeholder="Search by name…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320 }} />
      </div>

      <div className="card">
        <table>
          <thead>
            <tr><th>Name</th><th>Type</th><th>Status</th><th>Ver.</th><th>Uploaded</th></tr>
          </thead>
          <tbody>
            {isLoading && <tr><td className="muted">Loading…</td></tr>}
            {(data || []).map((d: any) => (
              <tr key={d.id}>
                <td><Link to={`/documents/${d.id}`}>{d.name}</Link></td>
                <td className="muted">{d.file_type?.toUpperCase()}</td>
                <td><StatusBadge status={d.processing_status} /></td>
                <td className="muted">v{d.current_version}</td>
                <td className="muted">{new Date(d.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr><td colSpan={5} className="muted">No documents yet — upload to begin.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
