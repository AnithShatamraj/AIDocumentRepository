import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";

/** Documents shown as links on a card before collapsing the rest to "+N more". */
const CARD_DOCS_SHOWN = 3;

export interface ResearchCardData {
  id: string;
  title: string;
  updated_at: string;
  summary: string;
  message_count: number;
  documents: { id: string; name: string }[];
  document_count: number;
}

function ResearchCard({
  r, onRename, onDelete,
}: {
  r: ResearchCardData;
  onRename: (r: ResearchCardData) => void;
  onDelete: (r: ResearchCardData) => void;
}) {
  const nav = useNavigate();
  const open = () => nav(`/research/${r.id}`);
  const shown = r.documents.slice(0, CARD_DOCS_SHOWN);
  const more = r.document_count - shown.length;
  return (
    <div
      className="card research-card"
      role="link"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => e.target === e.currentTarget && (e.key === "Enter" || e.key === " ") && (e.preventDefault(), open())}
    >
      <div className="research-card-head">
        <div className="research-card-title" title={r.title}>{r.title}</div>
        {/* Card actions must not also open the thread. */}
        <div className="research-card-actions" onClick={(e) => e.stopPropagation()}>
          <button className="hist-act" title="Rename" onClick={() => onRename(r)}>✎</button>
          <button className="hist-act" title="Delete" onClick={() => onDelete(r)}>✕</button>
        </div>
      </div>
      <div className="research-card-summary">
        {r.summary || <span className="muted">No answer yet.</span>}
      </div>
      <div className="research-card-docs">
        {shown.map((d) => (
          <Link key={d.id} to={`/documents/${d.id}`} className="cite" title={d.name}
                onClick={(e) => e.stopPropagation()}>
            {d.name}
          </Link>
        ))}
        {more > 0 && <span className="muted tiny-text">+{more} more</span>}
        {r.document_count === 0 && <span className="muted tiny-text">No documents referenced</span>}
      </div>
      <div className="research-card-foot muted">
        <span>{new Date(r.updated_at).toLocaleString()}</span>
        <span>{Math.ceil(r.message_count / 2)} question(s)</span>
      </div>
    </div>
  );
}

export function Research() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [err, setErr] = useState("");
  const { data, isLoading, error } = useQuery<ResearchCardData[]>({
    queryKey: ["conversations", search],
    queryFn: () => api.get(`/api/conversations${search ? `?q=${encodeURIComponent(search)}` : ""}`),
  });

  async function rename(r: ResearchCardData) {
    const title = window.prompt("Rename research", r.title);
    if (!title || title.trim() === r.title) return;
    setErr("");
    try {
      await api.patch(`/api/conversations/${r.id}`, { title: title.trim() });
      qc.invalidateQueries({ queryKey: ["conversations"] });
    } catch (e: any) {
      setErr(e.message);
    }
  }

  async function remove(r: ResearchCardData) {
    if (!window.confirm(`Delete “${r.title}”? This cannot be undone.`)) return;
    setErr("");
    try {
      await api.del(`/api/conversations/${r.id}`);
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    } catch (e: any) {
      setErr(e.message);
    }
  }

  return (
    <div>
      <div className="page-head spread">
        <div>
          <h1>Research</h1>
          <div className="muted">
            Your research threads, most recently modified first. Open one to pick up where you left off.
          </div>
        </div>
        <button onClick={() => nav("/research/new")}>+ New research</button>
      </div>

      <div className="toolbar">
        <input
          placeholder="Search research by title…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ maxWidth: 320 }}
        />
      </div>

      {isLoading && <div className="muted">Loading…</div>}
      {(error || err) && <div className="card err">{err || (error as Error).message}</div>}

      {data && data.length === 0 && (
        <div className="card muted">
          {search ? "No research matches this search." : (
            <>
              No research yet. Start one to ask questions across the documents you can read, with cited answers.{" "}
              <Link to="/research/new">Start new research</Link>
            </>
          )}
        </div>
      )}

      <div className="research-grid">
        {(data || []).map((r) => (
          <ResearchCard key={r.id} r={r} onRename={rename} onDelete={remove} />
        ))}
      </div>
    </div>
  );
}
