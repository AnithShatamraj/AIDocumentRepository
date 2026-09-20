import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import {
  FieldDefDraft,
  SchemaBuilder,
  emptyField,
  fromSchema,
  toPayload,
} from "../components/SchemaBuilder";

const STEPS = ["Describe", "Name", "Samples", "Fields", "Publish"];
const STAGE_INDEX: Record<string, number> = { describe: 0, name: 1, samples: 2, fields: 3 };
const PLACEHOLDER: Record<string, string> = {
  describe: "Describe the document type — or upload sample documents with 📎…",
  name: "Type a name of your own, or use the suggestion above…",
  samples: "Say “no samples” to skip, or upload some with 📎…",
  fields: "Ask the AI to change the fields — e.g. “add a due date”…",
};
const ACTION_TEXT: Record<string, string> = {
  describe_from_samples: "Write the description from my samples",
  confirm_samples: "They're the same type — continue",
  confirm_description: "Confirm description",
  accept_name: "Use this name",
  suggest_name: "Suggest another name",
  skip_samples: "No samples — skip",
  derive_fields: "Derive fields from my samples",
};
const ACCEPT = ".pdf,.docx,.pptx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.tif,.tiff";

/** Sorted-key JSON, so two structurally equal trees compare equal regardless
 * of the key order Postgres' JSONB hands back. */
function canon(x: any): string {
  if (Array.isArray(x)) return `[${x.map(canon).join(",")}]`;
  if (x && typeof x === "object") {
    return `{${Object.keys(x).sort().map((k) => `${JSON.stringify(k)}:${canon(x[k])}`).join(",")}}`;
  }
  return JSON.stringify(x);
}

/** The assistant writes **bold**; nothing else needs rendering. */
function Rich({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\*\*[^*]+\*\*)/g).map((p, i) =>
        p.startsWith("**") && p.endsWith("**") ? <strong key={i}>{p.slice(2, -2)}</strong> : <span key={i}>{p}</span>
      )}
    </>
  );
}

function Stepper({ stage, published }: { stage: string; published: boolean }) {
  const cur = published ? STEPS.length : STAGE_INDEX[stage] ?? 0;
  return (
    <div className="stepper">
      {STEPS.map((s, i) => (
        <span key={s} className={`step${i < cur ? " done" : ""}${i === cur ? " current" : ""}`}>
          {i < cur ? "✓ " : `${i + 1}. `}
          {s}
        </span>
      ))}
    </div>
  );
}

function ValidationResults({ results }: { results: Record<string, any> }) {
  const entries = Object.values(results || {}) as any[];
  if (!entries.length) return null;
  return (
    <div>
      {entries.map((r) => (
        <details key={r.sample_id} open style={{ marginBottom: 10 }}>
          <summary style={{ cursor: "pointer", fontSize: 13 }}>
            <strong>{r.filename}</strong>{" "}
            <span className={r.found === r.total ? "ok" : "muted"}>
              — {r.found} of {r.total} fields found
            </span>
          </summary>
          <div style={{ overflowX: "auto", marginTop: 6 }}>
            <table className="field-table">
              <thead>
                <tr><th>Field</th><th>Extracted value</th><th>Confidence</th></tr>
              </thead>
              <tbody>
                {r.rows.map((row: any, i: number) => (
                  <tr key={i}>
                    <td title={row.path}>{row.label}</td>
                    <td style={{ whiteSpace: "normal", maxWidth: 320 }}>
                      {row.value ?? <span className="err">not found</span>}
                    </td>
                    <td>
                      {row.value ? (
                        <span className={row.confidence < 0.6 ? "err" : "ok"}>{Math.round(row.confidence * 100)}%</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {r.missing?.length > 0 && (
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              Not found in this sample: {r.missing.join(", ")}
            </div>
          )}
        </details>
      ))}
    </div>
  );
}

export function DocumentTypeBuilder() {
  const { id } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();

  const [draft, setDraft] = useState<any | null>(null); // last server snapshot (messages, samples, stage…)
  const [loadErr, setLoadErr] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [fields, setFields] = useState<FieldDefDraft[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState<null | "message" | "validate" | "publish" | "upload">(null);
  const [pendingText, setPendingText] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<"saved" | "saving" | "error">("saved");
  const [err, setErr] = useState("");
  const [publishErr, setPublishErr] = useState(""); // shown beside the Publish button, where the admin is looking

  const chatEnd = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const editor = useRef({ name, description, fields });
  editor.current = { name, description, fields };
  const dirty = useRef(false);
  const saveTimer = useRef<number>();
  const saving = useRef<Promise<void>>(Promise.resolve());
  const pendingUpload = useRef<{ action: string; names: string[] } | null>(null);

  // ---------------------------------------------------------------- loading
  function hydrate(d: any, opts: { keepFields?: boolean } = {}) {
    setDraft(d);
    setName(d.name || "");
    setDescription(d.description || "");
    if (!opts.keepFields) setFields(fromSchema(d.fields || []));
  }

  useEffect(() => {
    let live = true;
    setDraft(null);
    setLoadErr("");
    api
      .get(`/api/document-type-drafts/${id}`)
      .then((d) => live && hydrate(d))
      .catch((e) => live && setLoadErr(e.message || "Could not load this draft."));
    return () => {
      live = false;
    };
  }, [id]);

  // -------------------------------------------------------------- autosave
  function saveNow(): Promise<void> {
    window.clearTimeout(saveTimer.current);
    if (!dirty.current) return saving.current;
    dirty.current = false;
    const snap = editor.current;
    saving.current = saving.current
      .then(() =>
        api.patch(`/api/document-type-drafts/${id}`, {
          name: snap.name,
          description: snap.description,
          fields: toPayload(snap.fields),
        })
      )
      .then(() => setSaveState(dirty.current ? "saving" : "saved"))
      .catch(() => {
        dirty.current = true;
        setSaveState("error");
      });
    return saving.current;
  }
  const saveNowRef = useRef(saveNow);
  saveNowRef.current = saveNow;

  function markDirty() {
    dirty.current = true;
    setSaveState("saving");
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => saveNowRef.current(), 700);
  }

  // Don't lose an edit made within the debounce window when navigating away.
  useEffect(() => () => void saveNowRef.current(), []);

  const editName = (v: string) => { setName(v); setPublishErr(""); markDirty(); };
  const editDescription = (v: string) => { setDescription(v); markDirty(); };
  const editFields = (v: FieldDefDraft[]) => { setFields(v); markDirty(); };

  // ------------------------------------------------- sample processing poll
  const samples: any[] = draft?.samples || [];
  const processing = samples.some((s) => s.status === "processing");
  useEffect(() => {
    if (!processing) return;
    const t = window.setInterval(async () => {
      try {
        const d = await api.get(`/api/document-type-drafts/${id}`);
        setDraft((cur: any) => (cur ? { ...cur, samples: d.samples, sample_check: d.sample_check } : cur));
      } catch {
        /* transient; the next tick retries */
      }
    }, 1500);
    return () => window.clearInterval(t);
  }, [processing, id]);

  // Once freshly uploaded samples finish, hand them to the AI automatically:
  // in the first step it writes the description from them, later it designs
  // (or improves) the fields.
  useEffect(() => {
    if (!pendingUpload.current || !samples.length || processing || busy) return;
    const { action, names } = pendingUpload.current;
    pendingUpload.current = null;
    if (samples.some((s) => s.status === "ready")) {
      send({
        action,
        label: `📎 Uploaded ${names.length} sample${names.length > 1 ? "s" : ""}: ${names.join(", ")}`,
      });
    } else {
      setErr("None of the uploaded files could be read — see the errors under Sample documents.");
    }
  }, [samples, processing, busy]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [draft?.messages?.length, pendingText, busy]);

  // ---------------------------------------------------------------- actions
  /** Adopt a server response after an AI turn. The editor was locked while it
   * ran, so the only fields to replace are ones the AI actually changed. */
  function adopt(d: any, sent: any[]) {
    const changed = canon(d.fields || []) !== canon(sent);
    hydrate(d, { keepFields: !changed });
  }

  async function send(body: { content?: string; action?: string; label?: string }) {
    if (busy || !draft) return;
    setErr("");
    setBusy("message");
    setPendingText(body.content || body.label || (body.action ? ACTION_TEXT[body.action] : "") || "");
    try {
      await saveNow();
      const sent = toPayload(editor.current.fields);
      adopt(await api.post(`/api/document-type-drafts/${id}/messages`, body), sent);
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setBusy(null);
      setPendingText(null);
    }
  }

  function submitText() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    send({ content: text });
  }

  async function uploadFiles(list: FileList | null) {
    if (!list?.length || busy) return;
    const files = Array.from(list);
    setErr("");
    setBusy("upload");
    try {
      const form = new FormData();
      files.forEach((f) => form.append("files", f));
      const r = await api.upload(`/api/document-type-drafts/${id}/samples`, form);
      pendingUpload.current = {
        action: draft?.stage === "describe" ? "describe_from_samples" : "derive_fields",
        names: files.map((f) => f.name),
      };
      setDraft((cur: any) => ({ ...cur, samples: r.samples, sample_check: null })); // a new set: any verdict is stale
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setBusy(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function removeSample(sid: string) {
    try {
      await api.del(`/api/document-type-drafts/${id}/samples/${sid}`);
      setDraft((cur: any) => ({
        ...cur,
        sample_check: null, // a different set now: the earlier verdict no longer applies
        samples: cur.samples.filter((s: any) => s.id !== sid),
        last_validation: Object.fromEntries(
          Object.entries(cur.last_validation || {}).filter(([k]) => k !== sid)
        ),
      }));
    } catch (e: any) {
      setErr(e.message || String(e));
    }
  }

  async function runValidate() {
    if (busy) return;
    setErr("");
    setBusy("validate");
    setPendingText("Test the fields on my sample");
    try {
      await saveNow();
      const sent = toPayload(editor.current.fields);
      adopt(await api.post(`/api/document-type-drafts/${id}/validate`, {}), sent);
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setBusy(null);
      setPendingText(null);
    }
  }

  async function publish() {
    if (busy) return;
    setErr("");
    setPublishErr("");
    setBusy("publish");
    try {
      await saveNow();
      const r = await api.post(`/api/document-type-drafts/${id}/publish`);
      qc.invalidateQueries({ queryKey: ["document-types"] });
      qc.invalidateQueries({ queryKey: ["document-type-details"] });
      qc.invalidateQueries({ queryKey: ["type-drafts"] });
      nav("/document-types", { state: { published: r.document_type.name } });
    } catch (e: any) {
      setPublishErr(e.message || String(e));
      setBusy(null);
    }
  }

  async function discard() {
    if (!window.confirm("Discard this draft? The conversation and any uploaded samples are deleted.")) return;
    try {
      window.clearTimeout(saveTimer.current);
      dirty.current = false;
      await api.del(`/api/document-type-drafts/${id}`);
      qc.invalidateQueries({ queryKey: ["type-drafts"] });
      nav("/document-types");
    } catch (e: any) {
      setErr(e.message || String(e));
    }
  }

  function onAction(action: string) {
    if (action === "upload_samples") fileInput.current?.click();
    else if (action === "validate") runValidate();
    else if (action === "publish") publish();
    else send({ action });
  }

  // ----------------------------------------------------------------- render
  if (loadErr)
    return (
      <div className="card">
        <div className="err">{loadErr}</div>
        <button className="secondary" style={{ marginTop: 12 }} onClick={() => nav("/document-types")}>
          ← Back to document types
        </button>
      </div>
    );
  if (!draft) return <div className="muted">Loading…</div>;

  const published = draft.status === "published";
  const stage: string = draft.stage;
  const messages: any[] = draft.messages || [];
  const lastIdx = messages.length - 1;
  const hasFields = toPayload(fields).length > 0;
  const readySamples = samples.filter((s) => s.status === "ready");
  // The AI's verdict on whether the samples are one kind of document (null if it
  // was about a different set of files), and whether it's an unresolved warning.
  const check = draft.sample_check;
  const flagged = !!check && check.related === false && !check.confirmed;
  const locked = busy === "message" || busy === "validate" || busy === "publish";
  const canPublish = stage === "fields" && hasFields && name.trim() !== "" && !busy && !published;
  const saveLabel = { saved: "All changes saved", saving: "Saving…", error: "Couldn't save — retrying on next edit" }[saveState];

  // Buttons under the latest assistant message. Samples that are already
  // uploaded and readable can always be handed to the AI -- this also covers
  // reopening a draft, or retrying after a failed attempt. In the first step
  // that means writing the description from them (until one exists); in the
  // samples step, designing the fields.
  const actionsFor = (m: any): any[] => {
    const base: any[] = m.payload?.actions || [];
    const has = (a: string) => base.some((b) => b.action === a);
    if (stage === "describe" && readySamples.length && !description.trim() && !has("describe_from_samples")) {
      return [{ action: "describe_from_samples", label: "Write the description from my samples" }, ...base];
    }
    if (stage === "samples" && readySamples.length && !has("derive_fields")) {
      return [{ action: "derive_fields", label: "Derive fields from my samples" }, ...base];
    }
    return base;
  };
  const canUpload = stage === "describe" || stage === "samples" || stage === "fields";

  return (
    <div className="builder-page">
      <div className="page-head spread">
        <div>
          <h1>New document type</h1>
          <div className="muted">Describe it to the AI, refine the fields together, then publish.</div>
        </div>
        <div className="row">
          <span className={saveState === "error" ? "err" : "muted"} style={{ fontSize: 12 }}>{saveLabel}</span>
          <button className="ghost" onClick={() => nav("/document-types")}>← Document types</button>
          {!published && (
            <button className="ghost reject" onClick={discard} disabled={!!busy}>Discard draft</button>
          )}
        </div>
      </div>
      <Stepper stage={stage} published={published} />

      <div className="builder">
        {/* ------------------------------------------------------------ chat */}
        <section className="card builder-chat">
          <div className="chat-scroll">
            {messages.map((m, i) => (
              <div key={m.id} className={`bubble ${m.role}${m.payload?.kind === "sample_mismatch" ? " warn" : ""}`}>
                {m.payload?.kind === "sample_mismatch" && (
                  <div className="warn-title">⚠ These samples may not belong together</div>
                )}
                <Rich text={m.content} />
                {m.payload?.kind === "description_proposal" && (
                  <div className="proposal">{m.payload.description}</div>
                )}
                {m.role === "assistant" && i === lastIdx && !busy && !published && actionsFor(m).length > 0 && (
                  <div className="chat-actions">
                    {actionsFor(m).map((a: any) => (
                      <button
                        key={a.action}
                        className={a.action === "publish" ? "" : "secondary"}
                        disabled={
                          (a.action === "publish" && !canPublish) ||
                          (a.action === "validate" && (!readySamples.length || !hasFields))
                        }
                        onClick={() => onAction(a.action)}
                      >
                        {a.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
            {pendingText && <div className="bubble user">{pendingText}</div>}
            {(busy === "message" || busy === "validate") && (
              <div className="bubble assistant typing" aria-label="The AI is working">
                <span /><span /><span />
              </div>
            )}
            {busy === "upload" && <div className="muted" style={{ fontSize: 12 }}>Uploading…</div>}
            {processing && !busy && (
              <div className="muted" style={{ fontSize: 12 }}>Reading your sample documents…</div>
            )}
            {err && <div className="err">{err}</div>}
            <div ref={chatEnd} />
          </div>

          <div className="composer">
            <input ref={fileInput} type="file" multiple accept={ACCEPT} hidden onChange={(e) => uploadFiles(e.target.files)} />
            <button
              className="ghost"
              title="Upload sample documents"
              disabled={!!busy || published || !canUpload}
              onClick={() => fileInput.current?.click()}
            >
              📎
            </button>
            <textarea
              rows={1}
              value={input}
              disabled={!!busy || published}
              placeholder={published ? "This draft has been published." : PLACEHOLDER[stage]}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submitText();
                }
              }}
            />
            <button onClick={submitText} disabled={!!busy || published || !input.trim()}>Send</button>
          </div>
        </section>

        {/* ---------------------------------------------------------- editor */}
        <section className="card builder-panel">
          <fieldset className="builder-lock" disabled={locked || published}>
            <div className="panel-scroll">
              <div className="grid cols-2" style={{ marginBottom: 4 }}>
                <div className="field">
                  <label>Name {draft.name_confirmed && <span className="ok">✓</span>}</label>
                  <input value={name} onChange={(e) => editName(e.target.value)} placeholder="Chosen in the chat" />
                </div>
                <div className="field" style={{ gridColumn: "1 / -1" }}>
                  <label>Description {draft.description_confirmed && <span className="ok">✓</span>}</label>
                  <textarea
                    rows={4}
                    value={description}
                    onChange={(e) => editDescription(e.target.value)}
                    placeholder="The AI writes this from what you tell it in the chat, or from your sample documents — you can edit it here too."
                  />
                </div>
              </div>

              {(canUpload || samples.length > 0) && (
                <div className="panel-block">
                  <div className="spread">
                    <strong>Sample documents</strong>
                    <button className="secondary" type="button" disabled={!!busy || samples.length >= 5}
                            onClick={() => fileInput.current?.click()}>
                      📎 Upload
                    </button>
                  </div>
                  {flagged && (
                    <div className="sample-warning">
                      These samples don't look like the same kind of document. Remove the ones that don't belong (✕),
                      or confirm in the chat that they really are the same type.
                    </div>
                  )}
                  {check && check.related === false && check.confirmed && (
                    <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                      You confirmed these are the same type of document.
                    </div>
                  )}
                  {samples.length === 0 ? (
                    <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                      None yet. Samples let the AI write the description and base the fields on real documents,
                      and let you test the extraction before publishing.
                    </div>
                  ) : (
                    samples.map((s) => {
                      const info = check?.documents?.find((d: any) => d.sample_id === s.id);
                      return (
                        <div key={s.id} className={`sample-row${flagged && info?.outlier ? " odd" : ""}`}>
                          <span style={{ minWidth: 0 }}>
                            <span title={s.filename}>📄 {s.filename}</span>
                            {info?.kind && <div className="sample-kind">{info.kind}</div>}
                          </span>
                          <span className="row" style={{ gap: 8 }}>
                            {flagged && info?.outlier && <span className="pill warn">looks different</span>}
                            {s.status === "processing" && <span className="pill">reading…</span>}
                            {s.status === "ready" && <span className="pill ok">ready</span>}
                            {s.status === "failed" && <span className="pill" style={{ color: "var(--danger)" }} title={s.error}>failed</span>}
                            <button className="ghost reject" type="button" onClick={() => removeSample(s.id)} title="Remove">✕</button>
                          </span>
                        </div>
                      );
                    })
                  )}
                  {samples.filter((s) => s.status === "failed").map((s) => (
                    <div key={s.id} className="err" style={{ fontSize: 12, marginTop: 4 }}>{s.filename}: {s.error}</div>
                  ))}
                </div>
              )}

              {stage === "fields" ? (
                <div className="panel-block">
                  <div className="spread" style={{ marginBottom: 8 }}>
                    <strong>Fields</strong>
                    <span className="muted" style={{ fontSize: 12 }}>
                      Edit directly, or ask the AI in the chat. <em>object</em> groups fields; <em>list</em> repeats.
                    </span>
                  </div>
                  <SchemaBuilder nodes={fields} onChange={editFields} />
                  <button className="secondary" type="button" onClick={() => editFields([...fields, emptyField()])}>
                    + Add field
                  </button>
                </div>
              ) : (
                <div className="panel-block muted" style={{ fontSize: 13 }}>
                  The extraction fields appear here once the description, name and samples are settled.
                </div>
              )}

              {stage === "fields" && (
                <div className="panel-block">
                  <div className="spread" style={{ marginBottom: 6 }}>
                    <strong>Test extraction</strong>
                    <button className="secondary" type="button" onClick={runValidate}
                            disabled={!!busy || !readySamples.length || !hasFields}>
                      ▶ Test on sample{readySamples.length > 1 ? "s" : ""}
                    </button>
                  </div>
                  {!readySamples.length ? (
                    <div className="muted" style={{ fontSize: 12 }}>
                      Upload a sample document to check what the AI would extract from it before publishing.
                    </div>
                  ) : !Object.keys(draft.last_validation || {}).length ? (
                    <div className="muted" style={{ fontSize: 12 }}>Not run yet.</div>
                  ) : (
                    <ValidationResults results={draft.last_validation} />
                  )}
                </div>
              )}
            </div>
          </fieldset>

          <div className="panel-foot">
            <span className={publishErr ? "err" : "muted"} style={{ fontSize: 12 }}>
              {publishErr
                ? publishErr
                : published
                ? `Published as “${draft.name}”.`
                : busy === "message" || busy === "validate"
                ? "The AI is working — editing is paused."
                : stage !== "fields"
                ? "Publishing unlocks once the fields step is reached."
                : !hasFields
                ? "Add at least one field to publish."
                : !name.trim()
                ? "Give the document type a name to publish."
                : "Ready to publish. Approval workflow can be added later."}
            </span>
            {published ? (
              <button onClick={() => nav("/document-types")}>Back to document types</button>
            ) : (
              <button onClick={publish} disabled={!canPublish}>
                {busy === "publish" ? "Publishing…" : "Publish document type"}
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
