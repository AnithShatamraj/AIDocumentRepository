import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, API_BASE, tokenStore } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { CollapsibleSection } from "../components/CollapsibleSection";
import { useLayoutNav } from "../components/Layout";
import { PdfViewer, Highlight } from "../components/PdfViewer";
import { FieldTree, FieldNode } from "../components/FieldTree";

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
  const [viewerUrl, setViewerUrl] = useState<string>("");
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const [hlNonce, setHlNonce] = useState(0);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [reviewing, setReviewing] = useState<Record<string, boolean>>({});
  const [bulkBusy, setBulkBusy] = useState(false);
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
  const [classificationsOpen, setClassificationsOpen] = useState(false);
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
        setClassificationsOpen(false);
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

  // ESC closes the summary modal.
  useEffect(() => {
    if (!summaryOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSummaryOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [summaryOpen]);

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

  async function bulkReview(action: "accept" | "reject") {
    const verb = action === "accept" ? "Accept" : "Reject";
    if (!window.confirm(`${verb} all remaining pending fields on this document?`)) return;
    setBulkBusy(true);
    try {
      await api.post(`/api/documents/${id}/extractions/bulk-review`, { action });
      await refreshAfterReview();
    } finally {
      setBulkBusy(false);
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
  const timelineOpen = timelineOverride ?? doc.processing_status === "processing";

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
            {doc.document_types?.map((c: any) => <span key={c.id} className="pill">{c.name}</span>)}
          </div>
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
            {(doc.fields || []).length === 0 ? (
              <div className="muted">No fields extracted yet.</div>
            ) : (
              <FieldTree
                nodes={doc.fields}
                isPdf={isPdf}
                reviewing={reviewing}
                onJump={jumpToField}
                onReview={reviewField}
                onEdit={editField}
              />
            )}
          </CollapsibleSection>

          <CollapsibleSection title="Classifications" open={classificationsOpen} onOpenChange={setClassificationsOpen}>
            {(doc.classifications || []).length === 0 && <div className="muted">Not classified yet.</div>}
            {(doc.classifications || []).map((c: any, i: number) => (
              <div key={i} className="spread" style={{ padding: "4px 0" }}>
                <span>{c.label} {c.source === "human" && <span className="pill">manual</span>}</span>
                <span className="muted conf">{Math.round((c.confidence || 0) * 100)}%</span>
              </div>
            ))}
          </CollapsibleSection>

          <CollapsibleSection
            title="Processing Timeline"
            open={timelineOpen}
            onOpenChange={setTimelineOverride}
            badge={
              pipeline?.run?.status === "running" ? <span className="pill">running</span> : undefined
            }
          >
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
          </CollapsibleSection>
        </div>
      </div>

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
