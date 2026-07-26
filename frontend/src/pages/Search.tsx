import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";

export function Search() {
  const [tab, setTab] = useState<"semantic" | "structured">("semantic");
  return (
    <div>
      <div className="page-head">
        <h1>Search</h1>
        <div className="muted">Every result is filtered by your read permissions.</div>
      </div>
      <div className="toolbar">
        <button className={tab === "semantic" ? "" : "secondary"} onClick={() => setTab("semantic")}>
          Semantic / Hybrid
        </button>
        <button className={tab === "structured" ? "" : "secondary"} onClick={() => setTab("structured")}>
          Structured (metadata)
        </button>
      </div>
      {tab === "semantic" ? <Semantic /> : <Structured />}
    </div>
  );
}

function Semantic() {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [hits, setHits] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      setHits(await api.post("/api/search", { query: q, mode, k: 10 }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <form className="row" onSubmit={run} style={{ marginBottom: 16 }}>
        <input placeholder="Search your documents…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={mode} onChange={(e) => setMode(e.target.value)} style={{ width: 140 }}>
          <option value="hybrid">Hybrid</option>
          <option value="vector">Vector</option>
          <option value="keyword">Keyword</option>
        </select>
        <button disabled={busy || !q}>{busy ? "…" : "Search"}</button>
      </form>
      <div className="stack">
        {hits.map((h) => (
          <div className="card" key={h.chunk_id}>
            <div className="spread">
              <Link to={`/documents/${h.document_id}`}><strong>{h.document_name}</strong></Link>
              <span className="muted">
                {h.page ? `p.${h.page}` : h.anchor} · score {h.score.toFixed(3)}
              </span>
            </div>
            <div className="muted" style={{ marginTop: 6, lineHeight: 1.5 }}>{h.content.slice(0, 400)}…</div>
          </div>
        ))}
        {hits.length === 0 && <div className="muted">Run a query to see results.</div>}
      </div>
    </div>
  );
}

function Structured() {
  const [field, setField] = useState("Expiration Date");
  const [op, setOp] = useState("before");
  const [value, setValue] = useState("");
  const [verifiedOnly, setVerifiedOnly] = useState(false);
  const [res, setRes] = useState<any>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setRes(
      await api.post("/api/search/structured", {
        field_name: field || null, op, value: value || null, verified_only: verifiedOnly, limit: 100,
      })
    );
  }

  return (
    <div>
      <form className="card" onSubmit={run} style={{ marginBottom: 16 }}>
        <div className="grid cols-4">
          <div className="field">
            <label>Field</label>
            <input value={field} onChange={(e) => setField(e.target.value)} placeholder="e.g. Total Amount" />
          </div>
          <div className="field">
            <label>Operator</label>
            <select value={op} onChange={(e) => setOp(e.target.value)}>
              <option value="eq">equals</option>
              <option value="contains">contains</option>
              <option value="gt">greater than</option>
              <option value="lt">less than</option>
              <option value="before">before (date)</option>
              <option value="after">after (date)</option>
            </select>
          </div>
          <div className="field">
            <label>Value</label>
            <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="e.g. 2026-12-31 or 1000" />
          </div>
          <div className="field" style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
            <label style={{ margin: 0 }}>
              <input type="checkbox" style={{ width: "auto" }} checked={verifiedOnly}
                onChange={(e) => setVerifiedOnly(e.target.checked)} /> Verified only
            </label>
          </div>
        </div>
        <button>Query</button>
      </form>

      {res && (
        <div className="card">
          <div className="muted" style={{ marginBottom: 10 }}>
            {res.count} result(s){res.verified_only ? " · verified only" : ""}
          </div>
          <table>
            <thead>
              <tr><th>Document</th><th>Field</th><th>Value</th><th>Status</th><th>Conf.</th></tr>
            </thead>
            <tbody>
              {res.results.map((r: any, i: number) => (
                <tr key={i}>
                  <td><Link to={`/documents/${r.document_id}`}>{r.document_name}</Link></td>
                  <td>{r.field_name}</td>
                  <td>{r.raw_value}</td>
                  <td className="muted">{r.review_status}</td>
                  <td className="muted conf">{Math.round((r.confidence || 0) * 100)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
