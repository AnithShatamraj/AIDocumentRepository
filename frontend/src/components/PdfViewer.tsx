import { useCallback, useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { api } from "../api/client";

// Vite-friendly worker setup (bundled locally, no CDN).
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url
).toString();

export interface Highlight {
  page: number;
  rects: { x0: number; y0: number; x1: number; y1: number }[];
  page_width?: number | null;
  page_height?: number | null;
  match?: number;
}

interface Props {
  url: string;
  docId: string;
  version?: number;
  /** known page count (from ingestion) — used when pdf.js can't open the doc */
  pageCount?: number;
  highlight?: Highlight | null;
  /** bump this to re-trigger the flash animation for the same highlight */
  highlightNonce?: number;
}

/** After pdf.js reports success, verify it actually painted something —
 * pathological vector PDFs render silently blank; fall back to server raster. */
function canvasIsBlank(container: HTMLElement | null): boolean {
  const canvas = container?.querySelector("canvas");
  if (!canvas) return true;
  try {
    const ctx = canvas.getContext("2d");
    if (!ctx) return false;
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    for (let i = 0; i < data.length; i += 4 * 499) {
      if (data[i] < 240 || data[i + 1] < 240 || data[i + 2] < 240) return false;
    }
    return true;
  } catch {
    return false;
  }
}

export function PdfViewer({ url, docId, version, pageCount, highlight, highlightNonce }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const pageWrapRef = useRef<HTMLDivElement>(null);
  const [numPages, setNumPages] = useState(0);
  const [pageNumber, setPageNumber] = useState(1);
  const [width, setWidth] = useState(760);
  const [zoom, setZoom] = useState(1);
  const [pdfPageDims, setPdfPageDims] = useState<{ w: number; h: number } | null>(null);
  const [flash, setFlash] = useState(false);
  const [mode, setMode] = useState<"canvas" | "image">("canvas");
  const [imgUrl, setImgUrl] = useState<string>("");
  const [imgLoading, setImgLoading] = useState(false);
  // Page content is painted — safe to scroll a highlight into view.
  const [pageReady, setPageReady] = useState(false);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(320, el.clientWidth - 26)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (highlight?.page) {
      setPageNumber((p) => {
        if (p !== highlight.page) setPageReady(false); // wait for the new page to paint
        return highlight.page;
      });
      setFlash(true);
      const t = setTimeout(() => setFlash(false), 2600);
      return () => clearTimeout(t);
    }
  }, [highlight, highlightNonce]);

  // Re-render of the page invalidates "ready" until it paints again.
  useEffect(() => {
    setPageReady(false);
  }, [pageNumber, mode]);

  // Image mode: fetch the server-rendered page raster (authenticated).
  useEffect(() => {
    if (mode !== "image") return;
    let revoked: string | null = null;
    setImgLoading(true);
    api
      .blobUrl(`/api/documents/${docId}/pages/${pageNumber}/image${version ? `?version=${version}` : ""}`)
      .then((u) => {
        revoked = u;
        setImgUrl(u);
      })
      .catch(() => setImgUrl(""))
      .finally(() => setImgLoading(false));
    return () => {
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [mode, pageNumber, docId, version]);

  const onDocLoad = useCallback(({ numPages }: { numPages: number }) => {
    setNumPages(numPages);
    setPageNumber((p) => Math.min(p, numPages) || 1);
  }, []);

  const switchToImage = useCallback(() => setMode("image"), []);

  const onPageRender = useCallback(() => {
    setPageReady(true);
    // Silent-blank detection (give the paint a beat to commit).
    setTimeout(() => {
      if (canvasIsBlank(pageWrapRef.current)) switchToImage();
    }, 300);
  }, [switchToImage]);

  const totalPages = numPages || pageCount || 0;
  const renderedWidth = width * zoom;
  const showRects =
    highlight && highlight.page === pageNumber && (highlight.rects?.length ?? 0) > 0;
  const baseW = highlight?.page_width || pdfPageDims?.w || 612;

  // Scroll the highlighted region into view once its page has painted.
  useEffect(() => {
    if (!pageReady || !showRects) return;
    const scroller = containerRef.current;
    if (!scroller) return;
    const s = renderedWidth / baseW;
    const rects = highlight!.rects;
    const top = Math.min(...rects.map((r) => r.y0)) * s;
    const bottom = Math.max(...rects.map((r) => r.y1)) * s;
    // Page offset within the scroller (robust to offsetParent quirks).
    const pageTop = pageWrapRef.current
      ? pageWrapRef.current.getBoundingClientRect().top -
        scroller.getBoundingClientRect().top +
        scroller.scrollTop
      : 0;
    // Place the region ~1/4 down the viewport so surrounding context is visible.
    const target = pageTop + top - scroller.clientHeight / 4;
    const maxScroll = scroller.scrollHeight - scroller.clientHeight;
    const alreadyVisible =
      pageTop + top >= scroller.scrollTop &&
      pageTop + bottom <= scroller.scrollTop + scroller.clientHeight;
    if (!alreadyVisible) {
      scroller.scrollTo({ top: Math.max(0, Math.min(target, maxScroll)), behavior: "smooth" });
    }
  }, [pageReady, showRects, highlight, highlightNonce, renderedWidth, baseW]);

  const overlays = showRects
    ? highlight!.rects.map((r, i) => {
        const s = renderedWidth / baseW;
        return (
          <div
            key={`${highlightNonce}-${i}`}
            className={`hl-rect${flash ? " flash" : ""}`}
            style={{
              left: r.x0 * s,
              top: r.y0 * s,
              width: Math.max(4, (r.x1 - r.x0) * s),
              height: Math.max(4, (r.y1 - r.y0) * s),
            }}
          />
        );
      })
    : null;

  return (
    <div className="pdfviewer card" style={{ padding: 0, overflow: "hidden" }}>
      <div className="pdf-toolbar">
        <button className="ghost" disabled={pageNumber <= 1} onClick={() => setPageNumber((p) => p - 1)}>
          ‹ Prev
        </button>
        <span className="muted">
          Page{" "}
          <input
            type="number"
            min={1}
            max={totalPages || 1}
            value={pageNumber}
            onChange={(e) => {
              const v = parseInt(e.target.value, 10);
              if (!isNaN(v) && v >= 1 && (totalPages === 0 || v <= totalPages)) setPageNumber(v);
            }}
            style={{ width: 58, padding: "3px 6px", textAlign: "center" }}
          />{" "}
          of {totalPages || "…"}
        </span>
        <button className="ghost" disabled={totalPages > 0 && pageNumber >= totalPages} onClick={() => setPageNumber((p) => p + 1)}>
          Next ›
        </button>
        <span style={{ flex: 1 }} />
        <button className="ghost" onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.25).toFixed(2)))}>−</button>
        <span className="muted" style={{ minWidth: 44, textAlign: "center" }}>{Math.round(zoom * 100)}%</span>
        <button className="ghost" onClick={() => setZoom((z) => Math.min(3, +(z + 0.25).toFixed(2)))}>+</button>
        <button className="ghost" onClick={() => setZoom(1)}>Fit</button>
        <button
          className="ghost"
          title="Toggle between in-browser rendering and server-rendered images"
          onClick={() => setMode((m) => (m === "canvas" ? "image" : "canvas"))}
        >
          {mode === "canvas" ? "🖼" : "⚡"}
        </button>
      </div>

      <div ref={containerRef} className="pdf-scroll">
        <div ref={pageWrapRef} className="pdf-page-wrap" style={{ width: renderedWidth }}>
          {mode === "canvas" ? (
            <Document
              file={url}
              onLoadSuccess={onDocLoad}
              onLoadError={switchToImage}
              loading={<div className="muted" style={{ padding: 24 }}>Loading PDF…</div>}
              error={<div className="muted" style={{ padding: 24 }}>Switching to image mode…</div>}
            >
              <Page
                pageNumber={pageNumber}
                width={renderedWidth}
                renderTextLayer={false}
                renderAnnotationLayer={false}
                onRenderSuccess={onPageRender}
                onRenderError={switchToImage}
                onLoadSuccess={(page) => {
                  const vp = page.getViewport({ scale: 1 });
                  setPdfPageDims({ w: vp.width, h: vp.height });
                }}
              />
            </Document>
          ) : imgUrl ? (
            <img
              src={imgUrl}
              width={renderedWidth}
              alt={`Page ${pageNumber}`}
              style={{ display: "block" }}
              onLoad={() => setPageReady(true)}
            />
          ) : (
            <div className="muted" style={{ padding: 24 }}>
              {imgLoading ? "Rendering page…" : "Could not render this page."}
            </div>
          )}
          {overlays}
        </div>
      </div>

      {highlight && highlight.page === pageNumber && (highlight.rects?.length ?? 0) === 0 && (
        <div className="muted" style={{ padding: "6px 12px", fontSize: 12 }}>
          Source located on this page (no exact region available).
        </div>
      )}
    </div>
  );
}
