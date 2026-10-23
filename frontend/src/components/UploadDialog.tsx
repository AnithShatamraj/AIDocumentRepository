import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, API_BASE, tokenStore } from "../api/client";
import { TypePicker } from "./TypePicker";

interface Props {
  onClose: () => void;
  onUploaded: () => void;
}

type NameState = { checking: boolean; available: boolean | null; suggestion: string | null };

/** What happened to one file of a multi-file upload. */
interface FileOutcome {
  filename: string;
  ok: boolean;
  text: string;
}

const ACCEPT = ".pdf,.docx,.pptx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.tiff";

function actionText(r: any): string {
  if (r.action === "cloned") return "Identical content already existed — results reused.";
  if (r.action === "linked_existing") return r.message || "Identical document already existed — access granted.";
  return "Uploaded. The AI pipeline is running.";
}

export function UploadDialog({ onClose, onUploaded }: Props) {
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [typeId, setTypeId] = useState("auto");
  const [nameState, setNameState] = useState<NameState>({ checking: false, available: null, suggestion: null });
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<any>(null); // single-file response
  const [outcomes, setOutcomes] = useState<FileOutcome[] | null>(null); // multi-file results
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const single = files.length === 1;
  const multi = files.length > 1;

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
  // Only meaningful for one file: with several, each keeps its own file name.
  useEffect(() => {
    const candidate = name.trim();
    if (!single || !candidate) {
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
  }, [name, single]);

  function pick(list: FileList | File[] | null) {
    const picked = Array.from(list || []);
    setFiles(picked);
    setError("");
    // A single file defaults its document name to the file name; several
    // files keep their own names, so there is nothing to default.
    setName(picked.length === 1 ? picked[0].name : "");
  }

  async function post(file: File, docName: string | null) {
    const form = new FormData();
    form.append("file", file);
    form.append("description", description);
    if (docName) form.append("name", docName);
    form.append("document_type_id", typeId);
    const res = await fetch(`${API_BASE}/api/documents`, {
      method: "POST",
      headers: { Authorization: `Bearer ${tokenStore.get()}` },
      body: form,
    });
    const body = await res.json().catch(() => ({}));
    return { res, body };
  }

  async function submitOne() {
    const { res, body } = await post(files[0], name.trim() || null);
    if (res.status === 409) {
      const d = body.detail || {};
      setNameState({ checking: false, available: false, suggestion: d.suggestion || null });
      throw new Error(d.message || "That name is already taken.");
    }
    if (!res.ok) throw new Error(body.detail?.message || body.detail || "Upload failed");
    setResult(body);
  }

  async function submitMany() {
    const done: FileOutcome[] = [];
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      setProgress(`Uploading ${i + 1} of ${files.length}…`);
      try {
        let { res, body } = await post(f, null);
        // A name clash would stall a batch with no per-file confirmation, so
        // take the suggested name (same policy as the bulk endpoint).
        if (res.status === 409 && body.detail?.suggestion) {
          ({ res, body } = await post(f, body.detail.suggestion));
        }
        if (!res.ok) {
          const d = body.detail;
          done.push({ filename: f.name, ok: false, text: d?.message || (typeof d === "string" ? d : "Upload failed") });
        } else {
          done.push({ filename: f.name, ok: true, text: actionText(body) });
        }
      } catch (e: any) {
        done.push({ filename: f.name, ok: false, text: e.message || String(e) });
      }
    }
    setOutcomes(done);
  }

  async function submit() {
    if (!files.length) return setError("Choose at least one file first.");
    setBusy(true);
    setError("");
    try {
      if (single) await submitOne();
      else await submitMany();
      onUploaded();
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
      setProgress("");
    }
  }

  function reset() {
    setResult(null);
    setOutcomes(null);
    setFiles([]);
    setName("");
    setDescription("");
    setTypeId("auto");
  }

  const nameBad = single && nameState.available === false;
  const totalMb = files.reduce((n, f) => n + f.size, 0) / 1024 / 1024;
  const finished = !!result || !!outcomes;

  return (
    <div className="modal-overlay" onClick={() => !busy && onClose()}>
      <div className="modal-card upload-card" onClick={(e) => e.stopPropagation()}>
        <div className="spread" style={{ marginBottom: 16 }}>
          <h2 style={{ margin: 0 }}>Upload document(s)</h2>
          <button className="ghost" onClick={onClose} disabled={busy}>✕ Close</button>
        </div>

        {finished ? (
          <div className="stack">
            {result && (
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
            )}
            {outcomes && (
              <div className="card">
                <strong>
                  {outcomes.filter((o) => o.ok).length} of {outcomes.length} uploaded
                </strong>
                {outcomes.map((o, i) => (
                  <div key={i} style={{ marginTop: 8, fontSize: 13 }}>
                    <span className={o.ok ? "ok" : "err"}>{o.ok ? "✓" : "✕"}</span> {o.filename}
                    <div className="muted" style={{ fontSize: 12 }}>{o.text}</div>
                  </div>
                ))}
              </div>
            )}
            <div className="row">
              <button onClick={onClose}>Done</button>
              <button className="secondary" onClick={reset}>Upload more</button>
            </div>
          </div>
        ) : (
          <>
            <div
              className={`dropzone${dragging ? " over" : ""}${files.length ? " has-file" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                pick(e.dataTransfer.files);
              }}
              onClick={() => fileRef.current?.click()}
            >
              <input
                ref={fileRef}
                type="file"
                multiple
                style={{ display: "none" }}
                accept={ACCEPT}
                onChange={(e) => pick(e.target.files)}
              />
              {single && (
                <>
                  <strong>{files[0].name}</strong>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {(files[0].size / 1024 / 1024).toFixed(2)} MB · click to choose different file(s)
                  </div>
                </>
              )}
              {multi && (
                <>
                  <strong>{files.length} files selected</strong>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {totalMb.toFixed(2)} MB total · click to choose different file(s)
                  </div>
                </>
              )}
              {!files.length && (
                <>
                  <strong>Drop one or more files here, or click to browse</strong>
                  <div className="muted" style={{ fontSize: 12 }}>PDF, DOCX, PPTX, XLSX, CSV, TXT, images</div>
                </>
              )}
            </div>

            {multi && (
              <div className="file-list">
                {files.map((f, i) => (
                  <div key={i} className="file-list-row">
                    <span className="clip">{f.name}</span>
                    <span className="muted tiny-text nowrap">{(f.size / 1024 / 1024).toFixed(2)} MB</span>
                  </div>
                ))}
              </div>
            )}

            {single && (
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
            )}
            {multi && (
              <div className="muted tiny-text" style={{ margin: "12px 0" }}>
                Each document is named after its file. If a name is already taken, a numbered
                name is used instead.
              </div>
            )}

            <div className="field">
              <label>Document type{multi ? " (applies to all files)" : ""}</label>
              <TypePicker types={types || []} value={typeId} onChange={setTypeId} />
            </div>

            <div className="field">
              <label>Description (optional{multi ? ", applies to all files" : ""})</label>
              <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>

            {error && <div className="err" style={{ marginBottom: 10 }}>{error}</div>}

            <div className="row" style={{ borderTop: "1px solid var(--border)", paddingTop: 14 }}>
              <button onClick={submit} disabled={busy || !files.length || nameBad}>
                {busy ? progress || "Uploading…" : multi ? `Upload ${files.length} files` : "Upload"}
              </button>
              <button className="ghost" onClick={onClose} disabled={busy}>Cancel</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
