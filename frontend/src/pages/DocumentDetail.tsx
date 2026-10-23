import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, API_BASE, tokenStore } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { CollapsibleSection } from "../components/CollapsibleSection";
import { useLayoutNav } from "../components/Layout";
import { PdfViewer, Highlight } from "../components/PdfViewer";
import { FieldNode } from "../components/FieldTree";
import { FieldSections } from "../components/FieldSections";
import { PromoteDialog, PromoteResult } from "../components/PromoteDialog";
import { useAuth } from "../auth/AuthContext";
import { TagEditor } from "../components/TagEditor";

const STAGE_LABEL: Record<string, string> = {
  text_extraction: "Text Extraction",
  document_understanding: "Document Understanding",
  classification: "Classification",
  summarization: "Summarization",
  metadata_extraction: "Metadata Extraction",
  chunking: "Chunking",
  embedding: "Embedding",
};

const PANEL_MIN = 280;
const PANEL_MAX = 720;

export function DocumentDetail() {
  const { id } = useParams();
  const qc = useQueryClient();
  const { user } = useAuth();
  // Adding a field to a document type edits the type, which is admin-only.
  const isAdmin = user?.role === "administrator";
  const [promote, setPromote] = useState<{ nodes: FieldNode[]; thenAcceptRest: boolean } | null>(null);
  const [notice, setNotice] = useState("");
  const [viewerUrl, setViewerUrl] = useState<string>("");
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const [hlNonce, setHlNonce] = useState(0);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [reviewing, setReviewing] = useState<Record<string, boolean>>({});
  const [bulkBusy, setBulkBusy] = useState(false);
  const [tagErr, setTagErr] = useState("");
  const [panelW, setPanelW] = useState(() => {
    const saved = parseInt(localStorage.getItem("aidocs_panel_w") || "380", 10);
    return isNaN(saved) ? 380 : Math.min(PANEL_MAX, Math.max(PANEL_MIN, saved));
  });
  const [dragging, setDragging] = useState(false);
  const workspaceRef = useRef<HTMLDivElement>(null);

  // Section open/closed state, lifted out of CollapsibleSection so "Focus
  // mode" can force them from here. timelineOpen keeps its old
  // still-processing-defaults-open behavior until the user (or focus mode)
  // explicitly overrides it.
  const [summarySectionOpen, setSummarySectionOpen] = useState(true);
  const [extractedOpen, setExtractedOpen] = useState(true);
  const [timelineOverride, setTimelineOverride] = useState<boolean | null>(null);

  // Focus mode: collapse the nav sidebar + every side-panel section except
  // Extracted Data, and widen the panel, so the document + extracted fields
  // get the most screen space. Restores whatever was there before on exit.
  const { navCollapsed, setNavCollapsed, setChromeHidden } = useLayoutNav();
  const [focusMode, setFocusMode] = useState(false);
  const prevNavCollapsed = useRef<boolean | null>(null);
  const prevPanelW = useRef<number | null>(null);

  function toggleFocusMode() {
    setFocusMode((f) => {
      const next = !f;
      setChromeHidden(next);
      if (next) {
        prevNavCollapsed.current = navCollapsed;
        prevPanelW.current = panelW;
        setNavCollapsed(true);
        setPanelW(PANEL_MAX);
        setSummarySectionOpen(false);
        setTimelineOverride(false);
        setExtractedOpen(true);
      } else {
        if (prevNavCollapsed.current !== null) setNavCollapsed(prevNavCollapsed.current);
        if (prevPanelW.current !== null) setPanelW(prevPanelW.current);
      }
      return next;
    });
  }

  // Safety net: if this page unmounts while focus mode is still on (nav away
  // without clicking Exit Focus, browser back, etc.), chromeHidden lives in
  // Layout's shared context -- leaving it on would hide the topbar on every
  // other page with no way back. Always clear it on unmount.
  useEffect(() => () => setChromeHidden(false), [setChromeHidden]);

  const { data: doc } = useQuery({
    queryKey: ["document", id],
    queryFn: () => api.get(`/api/documents/${id}`),
    refetchInterval: (q) =>
      ["processing", "pending"].includes((q.state.data as any)?.processing_status) ? 3000 : false,
  });
  const { data: pipeline } = useQuery({
    queryKey: ["pipeline", id],
    queryFn: () => api.get(`/api/documents/${id}/pipeline`),
    refetchInterval: (q) => ((q.state.data as any)?.run?.status === "running" ? 2000 : false),
  });

  const isPdf = doc?.file_type === "pdf";

  // Live pipeline events over SSE (in addition to polling).
  useEffect(() => {
    if (!id) return;
    const es = new EventSource(`${API_BASE}/api/documents/${id}/events?token=${tokenStore.get()}`);
    es.addEventListener("stage", () => {
      qc.invalidateQueries({ queryKey: ["pipeline", id] });
      qc.invalidateQueries({ queryKey: ["document", id] });
    });
    es.onerror = () => es.close();
    return () => es.close();
  }, [id]);

  // Auto-load the inline preview URL for PDFs.
  useEffect(() => {
    if (!id || !isPdf) return;
    api
      .get(`/api/documents/${id}/download?disposition=inline`)
      .then((res) => setViewerUrl(res.url))
      .catch(() => setViewerUrl(""));
  }, [id, isPdf, doc?.current_version]);

  // ESC closes whichever modal is open.
  useEffect(() => {
    if (!summaryOpen && !detailsOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setSummaryOpen(false);
        setDetailsOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [summaryOpen, detailsOpen]);

  // Splitter drag (pointer events cover mouse + touch): resize the right
  // panel against the workspace's right edge.
  const startDrag = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    setDragging(true);
    const onMove = (ev: PointerEvent) => {
      const rect = workspaceRef.current?.getBoundingClientRect();
      if (!rect) return;
      const w = Math.round(Math.min(PANEL_MAX, Math.max(PANEL_MIN, rect.right - ev.clientX)));
      setPanelW(w);
    };
    const onUp = () => {
      setDragging(false);
      window.removeEventListener("pointermove", onMove);
      setPanelW((w) => {
        localStorage.setItem("aidocs_panel_w", String(w));
        return w;
      });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
  }, []);

  async function openExternally() {
    const res = await api.get(`/api/documents/${id}/download`);
    window.open(res.url, "_blank");
  }

  async function reprocess() {
    await api.post(`/api/documents/${id}/reprocess`);
    qc.invalidateQueries({ queryKey: ["pipeline", id] });
  }

  // A citation on the Research page hands the highlight over in router state,
  // so clicking [2] opens this document at the block the answer came from
  // rather than dropping the reader on page 1. Re-applied when the viewer URL
  // resolves, since the PDF may not have loaded when we first arrive.
  const location = useLocation();
  useEffect(() => {
    const handed = (location.state as any)?.highlight;
    if (handed?.page) {
      setHighlight(handed);
      setHlNonce((n) => n + 1);
    }
  }, [location.state, viewerUrl]);

  function jumpToField(e: any) {
    if (e.source_bbox && e.source_bbox.page) {
      setHighlight(e.source_bbox);
      setHlNonce((n) => n + 1);
    } else if (e.source_page) {
      setHighlight({ page: e.source_page, rects: [] });
      setHlNonce((n) => n + 1);
    }
  }

  async function refreshAfterReview() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["document", id] }),
      qc.invalidateQueries({ queryKey: ["review"] }),
      qc.invalidateQueries({ queryKey: ["dashboard"] }),
    ]);
  }

  async function reviewField(extractionId: string, action: "accept" | "reject") {
    setReviewing((r) => ({ ...r, [extractionId]: true }));
    try {
      await api.post(`/api/documents/${id}/extractions/${extractionId}/review`, { action });
      await refreshAfterReview();
    } finally {
      setReviewing((r) => {
        const next = { ...r };
        delete next[extractionId];
        return next;
      });
    }
  }

  async function editField(extractionId: string, value: string) {
    setReviewing((r) => ({ ...r, [extractionId]: true }));
    try {
      await api.post(`/api/documents/${id}/extractions/${extractionId}/edit`, { value });
      await refreshAfterReview();
    } finally {
      setReviewing((r) => {
        const next = { ...r };
        delete next[extractionId];
        return next;
      });
    }
  }

  /** New (discovered) fields still waiting for a decision, one per distinct
   * fact -- the same fact is often found once per document type. */
  function pendingNewFields(): FieldNode[] {
    const seen = new Set<string>();
    const out: FieldNode[] = [];
    for (const f of (doc?.fields || []) as FieldNode[]) {
      if (f.is_discovered && f.review_status === "pending_review" && !seen.has(f.field_key)) {
        seen.add(f.field_key);
        out.push(f);
      }
    }
    return out;
  }

  /** Accept everything pending except new fields: those can only be accepted by
   * giving them a document type. */
  async function acceptRest() {
    setBulkBusy(true);
    try {
      await api.post(`/api/documents/${id}/extractions/bulk-review`, { action: "accept", skip_new: true });
      await refreshAfterReview();
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkReview(action: "accept" | "reject") {
    if (action === "accept") {
      const fresh = pendingNewFields();
      // Administrators decide where the new fields go first; the rest is accepted after.
      if (fresh.length && isAdmin) {
        setPromote({ nodes: fresh, thenAcceptRest: true });
        return;
      }
      const left = fresh.length
        ? ` The ${fresh.length} new field${fresh.length === 1 ? "" : "s"} will be left for an administrator to add to a document type.`
        : "";
      if (!window.confirm(`Accept all remaining pending fields on this document?${left}`)) return;
      await acceptRest();
      return;
    }
    if (!window.confirm("Reject all remaining pending fields on this document?")) return;
    setBulkBusy(true);
    try {
      await api.post(`/api/documents/${id}/extractions/bulk-review`, { action });
      await refreshAfterReview();
    } finally {
      setBulkBusy(false);
    }
  }

  async function onPromoted(results: PromoteResult[]) {
    const thenRest = promote?.thenAcceptRest;
    setPromote(null);
    const added = results.filter((r) => r.created_field);
    const mapped = results.length - added.length;
    const types = [...new Set(results.map((r) => r.document_type_name))].join(", ");
    setNotice(
      [added.length && `Added ${added.length} field${added.length === 1 ? "" : "s"} to ${types}`,
       mapped && `${mapped} mapped to existing field${mapped === 1 ? "" : "s"}`]
        .filter(Boolean).join(" · ") +
        (added.length ? ". Reprocess other documents of this type to extract it there too." : ".")
    );
    qc.invalidateQueries({ queryKey: ["document-types"] });
    qc.invalidateQueries({ queryKey: ["document-type-detail"] });
    if (thenRest) await acceptRest();
    else await refreshAfterReview();
  }

  async function saveTags(names: string[]) {
    setTagErr("");
    try {
      await api.put(`/api/documents/${id}/tags`, { tags: names });
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["document", id] }),
        // The tag list carries per-caller document counts, so it goes stale
        // the moment a document gains or loses one.
        qc.invalidateQueries({ queryKey: ["tags"] }),
        qc.invalidateQueries({ queryKey: ["documents"] }),
      ]);
    } catch (e: any) {
      setTagErr(e.message);
    }
  }

  async function deleteItem(extractionId: string) {
    if (!window.confirm("Delete this item? This cannot be undone.")) return;
    setReviewing((r) => ({ ...r, [extractionId]: true }));
    try {
      await api.del(`/api/documents/${id}/extractions/${extractionId}`);
      await refreshAfterReview();
    } finally {
      setReviewing((r) => {
        const next = { ...r };
        delete next[extractionId];
        return next;
      });
    }
  }

  if (!doc) return <div className="muted">Loading…</div>;

  const extraction = pipeline?.stages?.find((s: any) => s.name === "text_extraction");
  const ocrUsed = extraction?.output?.ocr_used;
  const parser = extraction?.output?.parser;
  const countPending = (nodes: any[]): number =>
    (nodes || []).reduce(
      (n, x) => n + (x.review_status === "pending_review" && x.node_kind === "scalar" ? 1 : 0) + countPending(x.children),
      0
    );
  const pendingCount = countPending(doc.fields || []);
  // The timeline earns panel space only while the document is being processed or
  // has a problem. `needs_review` counts as finished: processing is done, the
  // reviewer's work is the extracted data.
  const failed = doc.processing_status === "failed" || (pipeline?.stages || []).some((s: any) => s.status === "failed");
  const inProgress = ["pending", "processing"].includes(doc.processing_status);
  const timelineNeeded = inProgress || failed;
  const timelineOpen = timelineOverride ?? timelineNeeded;
  // Stage list + run totals, shown in the side panel while processing and in the
  // "Processing details" popup afterwards.
  const timelineBody = (
    <>
      <div className="timeline">
        {(pipeline?.stages || []).map((s: any) => (
          <div className="stage" key={s.name}>
            <span className={`dot ${s.status}`} />
            <div style={{ flex: 1 }}>
              <div className="spread">
                <span>{STAGE_LABEL[s.name] || s.name}</span>
                <span className="muted" style={{ fontSize: 12 }}>
                  {s.status}
                  {s.latency_ms != null ? ` · ${s.latency_ms}ms` : ""}
                  {s.cost_usd ? ` · $${s.cost_usd.toFixed(4)}` : ""}
                </span>
              </div>
              {s.error_message && <div className="err" style={{ fontSize: 12 }}>{s.error_message}</div>}
            </div>
          </div>
        ))}
        {(!pipeline?.stages || pipeline.stages.length === 0) && (
          <div className="muted">No pipeline run yet.</div>
        )}
      </div>
      {pipeline?.run && (
        <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          Run cost ${pipeline.run.total_cost_usd?.toFixed(4)} · {pipeline.run.total_tokens} tokens
        </div>
      )}
    </>
  );


  return (
    <div className={`doc-page${focusMode ? " focus" : ""}`}>
      <div className="page-head spread">
        <div>
          <h1>{doc.name}</h1>
          <div className="row muted">
            <StatusBadge status={doc.processing_status} />
            <span>{doc.file_type?.toUpperCase()} · v{doc.current_version}</span>
            {parser && <span className="pill">{parser}</span>}
            {ocrUsed && <span className="pill">OCR</span>}
            {!timelineNeeded && pipeline?.run && (
              <button className="ghost tiny" onClick={() => setDetailsOpen(true)}
                      title="Stage timings, cost and tokens for the last processing run">
                Processing details
              </button>
            )}
          </div>
          {/* Document types and tags share one row to leave the height to the
              document. Types come first; the hover text keeps what the old
              Classifications section showed: how sure the AI was, or that a
              person set it. */}
          <div className="tag-row meta-row">
            <div className="meta-group">
              <span className="muted tag-label">Document types</span>
              {(doc.document_types || []).length === 0 && (
                <span className="muted" style={{ fontSize: 13 }}>
                  {inProgress ? "Classifying…" : "Not classified"}
                </span>
              )}
              {(doc.document_types || []).map((t: any) => {
                const c = (doc.classifications || []).find((x: any) => x.label === t.name);
                const hint = !c ? undefined
                  : c.source === "human" ? "Set manually"
                  : `AI confidence ${Math.round((c.confidence || 0) * 100)}%`;
                return <span key={t.id} className="pill" title={hint}>{t.name}</span>;
              })}
            </div>
            <div className="meta-group meta-tags">
              <span className="muted tag-label">Tags</span>
              <TagEditor
                value={(doc.tags || []).map((t: any) => t.name)}
                onChange={saveTags}
              />
            </div>
          </div>
          {tagErr && <div className="muted tag-error">{tagErr}</div>}
        </div>
        <div className="row">
          <button className={focusMode ? "" : "ghost"} onClick={toggleFocusMode}
                  title="Collapse the nav and other sections to give the document and extracted data more room">
            {focusMode ? "⤢ Exit Focus" : "⛶ Focus Mode"}
          </button>
          <button className="secondary" onClick={openExternally}>Download</button>
          <button className="ghost" onClick={reprocess}>Reprocess</button>
        </div>
      </div>

      <div className="doc-workspace" ref={workspaceRef}>
        {/* Document — always in view */}
        <div className="doc-main">
          {isPdf ? (
            viewerUrl ? (
              <PdfViewer
                url={viewerUrl}
                docId={id!}
                version={doc.current_version}
                pageCount={extraction?.output?.units}
                highlight={highlight}
                highlightNonce={hlNonce}
              />
            ) : (
              <div className="card muted" style={{ flex: 1 }}>Loading preview…</div>
            )
          ) : (
            <div className="card" style={{ flex: 1 }}>
              <div className="muted">In-browser preview is available for PDFs.</div>
              <a onClick={openExternally} style={{ cursor: "pointer" }}>
                Download / open this {doc.file_type}
              </a>
            </div>
          )}
        </div>

        <div className={`splitter${dragging ? " dragging" : ""}`} onPointerDown={startDrag} title="Drag to resize" />

        {/* Right panel — collapsible sections, each scrolling internally */}
        <div className="side-panel" style={{ width: panelW }}>
          <CollapsibleSection
            title="AI Summary"
            open={summarySectionOpen}
            onOpenChange={setSummarySectionOpen}
            badge={<span className="pill">AI-generated</span>}
            actions={
              doc.summary && (
                <button className="ghost" title="Expand to full screen" onClick={() => setSummaryOpen(true)}>
                  ⛶
                </button>
              )
            }
          >
            <SummaryContent summary={doc.summary} />
          </CollapsibleSection>

          <CollapsibleSection
            title="Extracted Data"
            open={extractedOpen}
            onOpenChange={setExtractedOpen}
            badge={pendingCount > 0 ? <span className="pill">{pendingCount} pending</span> : undefined}
            actions={
              pendingCount > 0 && (
                <span className="row" style={{ gap: 6 }}>
                  <button className="ghost approve" disabled={bulkBusy} onClick={() => bulkReview("accept")}>
                    ✓ Accept All Remaining
                  </button>
                  <button className="ghost reject" disabled={bulkBusy} onClick={() => bulkReview("reject")}>
                    ✕ Reject All Remaining
                  </button>
                </span>
              )
            }
          >
            {notice && (
              <div className="fs-notice">
                <span>{notice}</span>
                <button className="ghost tiny" onClick={() => setNotice("")}>✕</button>
              </div>
            )}
            <FieldSections
              fields={(doc.fields || []) as FieldNode[]}
              docTypes={(doc.document_types || []).map((t: any) => ({ id: t.id, name: t.name }))}
              isPdf={isPdf}
              reviewing={reviewing}
              onJump={jumpToField}
              onReview={reviewField}
              onEdit={editField}
              onDelete={deleteItem}
              canPromote={isAdmin}
              onPromote={(nodes) => setPromote({ nodes, thenAcceptRest: false })}
            />
          </CollapsibleSection>

          {/* Only while it matters: running, queued, or something went wrong.
              A finished document gives this space to Summary and Extracted Data;
              the same detail is behind "Processing details" in the header. */}
          {timelineNeeded && (
            <CollapsibleSection
              title="Processing Timeline"
              open={timelineOpen}
              onOpenChange={setTimelineOverride}
              badge={
                pipeline?.run?.status === "running" ? <span className="pill">running</span> :
                failed ? <span className="pill" style={{ color: "var(--danger)" }}>failed</span> : undefined
              }
            >
              {timelineBody}
            </CollapsibleSection>
          )}
        </div>
      </div>

      {promote && (
        <PromoteDialog
          docId={id!}
          nodes={promote.nodes}
          docTypes={(doc.document_types || []).map((t: any) => ({ id: t.id, name: t.name }))}
          alternative={promote.thenAcceptRest
            ? { label: "Accept the rest, leave these as new", onClick: () => { setPromote(null); void acceptRest(); } }
            : undefined}
          onClose={() => setPromote(null)}
          onDone={onPromoted}
        />
      )}

      {/* Processing details: out of the side panel once a document is done. */}
      {detailsOpen && (
        <div className="modal-overlay" onClick={() => setDetailsOpen(false)}>
          <div className="modal-card sm" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Processing details">
            <div className="spread" style={{ marginBottom: 12 }}>
              <h2 style={{ margin: 0 }}>Processing details</h2>
              <button className="ghost" onClick={() => setDetailsOpen(false)}>✕ Close</button>
            </div>
            {timelineBody}
          </div>
        </div>
      )}

      {/* Fullscreen summary modal */}
      {summaryOpen && (
        <div className="modal-overlay" onClick={() => setSummaryOpen(false)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="spread" style={{ marginBottom: 12 }}>
              <h2 style={{ margin: 0 }}>
                AI Summary — {doc.name} <span className="pill">AI-generated</span>
              </h2>
              <button className="ghost" onClick={() => setSummaryOpen(false)}>✕ Close</button>
            </div>
            <SummaryContent summary={doc.summary} />
          </div>
        </div>
      )}
    </div>
  );
}

function SummaryContent({ summary }: { summary: any }) {
  if (!summary) return <div className="muted">Summary pending.</div>;
  return (
    <>
      <p style={{ lineHeight: 1.6 }}>{summary.executive_summary}</p>
      <ul className="muted" style={{ paddingLeft: 18 }}>
        {(summary.highlights || []).map((h: string, i: number) => <li key={i}>{h}</li>)}
      </ul>
    </>
  );
}

function ReviewStatus({ status }: { status: string }) {
  const map: Record<string, string> = {
    auto_accepted: "ok", verified: "ok", corrected: "ok",
    pending_review: "muted", rejected: "err",
  };
  return <span className={map[status] || "muted"} style={{ fontSize: 12 }}>{status.replace("_", " ")}</span>;
}
