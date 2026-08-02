import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, API_BASE, tokenStore } from "../api/client";
import { TypePicker } from "./TypePicker";

interface Props {
  onClose: () => void;
  onUploaded: () => void;
}

type NameState = { checking: boolean; available: boolean | null; suggestion: string | null };

export function UploadDialog({ onClose, onUploaded }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [typeId, setTypeId] = useState("auto");
  const [nameState, setNameState] = useState<NameState>({ checking: false, available: null, suggestion: null });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<any>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: types } = useQuery({
    queryKey: ["document-types"],
    queryFn: () => api.get("/api/document-types"),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  // Debounced uniqueness check — the API rejects duplicate names on submit.
  useEffect(() => {
    const candidate = name.trim();
    if (!candidate) {
      setNameState({ checking: false, available: null, suggestion: null });
      return;
    }
    setNameState((s) => ({ ...s, checking: true }));
    const t = setTimeout(async () => {
      try {
        const res = await api.get(`/api/documents/name-available?name=${encodeURIComponent(candidate)}`);
        setNameState({ checking: false, available: res.available, suggestion: res.suggestion });
      } catch {
        setNameState({ checking: false, available: null, suggestion: null });
      }
    }, 400);
    return () => clearTimeout(t);
  }, [name]);

  function pick(f: File | null) {
    setFile(f);
    setError("");
    if (f && !name.trim()) setName(f.name);
  }

  async function submit() {
    if (!file) return setError("Choose a file first.");
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("description", description);
      if (name.trim()) form.append("name", name.trim());
      form.append("document_type_id", typeId);

      const res = await fetch(`${API_BASE}/api/documents`, {
        method: "POST",
        headers: { Authorization: `Bearer ${tokenStore.get()}` },
        body: form,
      });
      const body = await res.json().catch(() => ({}));
      if (res.status === 409) {
        const d = body.detail || {};
        setNameState({ checking: false, available: false, suggestion: d.suggestion || null });
        throw new Error(d.message || "That name is already taken.");
      }
      if (!res.ok) throw new Error(body.detail?.message || body.detail || "Upload failed");

      setResult(body);
      onUploaded();
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  const nameBad = nameState.available === false;

  return (
    <div className="modal-overlay" onClick={() => !busy && onClose()}>
      <div className="modal-card upload-card" onClick={(e) => e.stopPropagation()}>
        <div className="spread" style={{ marginBottom: 16 }}>
          <h2 style={{ margin: 0 }}>Upload document</h2>
          <button className="ghost" onClick={onClose} disabled={busy}>✕ Close</button>
        </div>

        {result ? (
          <div className="stack">
            <div className="card">
              <strong>{result.document?.name}</strong>
              <div className="muted" style={{ marginTop: 6 }}>
                {result.action === "cloned" && "Identical content already existed — its processing results were reused, so this cost nothing to process."}
                {result.action === "linked_existing" && result.message}
                {result.action === "new" && "Uploaded. The AI pipeline is running — extraction appears as it completes."}
              </div>
              {result.message && result.action === "cloned" && (
                <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>{result.message}</div>
              )}
            </div>
            <div className="row">
              <button onClick={onClose}>Done</button>
              <button
                className="secondary"
                onClick={() => { setResult(null); setFile(null); setName(""); setDescription(""); setTypeId("auto"); }}
              >
                Upload another
              </button>
            </div>
          </div>
        ) : (
          <>
            <div
              className={`dropzone${dragging ? " over" : ""}${file ? " has-file" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                pick(e.dataTransfer.files?.[0] || null);
              }}
              onClick={() => fileRef.current?.click()}
            >
              <input
                ref={fileRef}
                type="file"
                style={{ display: "none" }}
                accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.tiff"
                onChange={(e) => pick(e.target.files?.[0] || null)}
              />
              {file ? (
                <>
                  <strong>{file.name}</strong>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {(file.size / 1024 / 1024).toFixed(2)} MB · click to choose a different file
                  </div>
                </>
              ) : (
                <>
                  <strong>Drop a file here, or click to browse</strong>
                  <div className="muted" style={{ fontSize: 12 }}>PDF, DOCX, PPTX, XLSX, CSV, TXT, images</div>
                </>
              )}
            </div>

            <div className="field" style={{ marginTop: 16 }}>
              <label>Document name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Defaults to the file name"
                style={nameBad ? { borderColor: "var(--danger)" } : undefined}
              />
              <div style={{ fontSize: 12, marginTop: 4, minHeight: 18 }}>
                {nameState.checking && <span className="muted">Checking availability…</span>}
                {!nameState.checking && nameState.available === true && (
                  <span className="ok">✓ Name is available</span>
                )}
                {!nameState.checking && nameBad && (
                  <span className="err">
                    Already used.{" "}
                    {nameState.suggestion && (
                      <a style={{ cursor: "pointer" }} onClick={() => setName(nameState.suggestion!)}>
                        Use “{nameState.suggestion}”
                      </a>
                    )}
                  </span>
                )}
              </div>
            </div>

            <div className="field">
              <label>Document type</label>
              <TypePicker types={types || []} value={typeId} onChange={setTypeId} />
            </div>

            <div className="field">
              <label>Description (optional)</label>
              <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>

            {error && <div className="err" style={{ marginBottom: 10 }}>{error}</div>}

            <div className="row" style={{ borderTop: "1px solid var(--border)", paddingTop: 14 }}>
              <button onClick={submit} disabled={busy || !file || nameBad}>
                {busy ? "Uploading…" : "Upload"}
              </button>
              <button className="ghost" onClick={onClose} disabled={busy}>Cancel</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
