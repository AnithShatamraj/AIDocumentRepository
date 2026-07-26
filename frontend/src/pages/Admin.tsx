import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export function Admin() {
  const { data: cfg } = useQuery({ queryKey: ["admin-config"], queryFn: () => api.get("/api/admin/config") });
  const { data: prompts } = useQuery({ queryKey: ["admin-prompts"], queryFn: () => api.get("/api/admin/prompts") });
  const [thresholds, setThresholds] = useState<string>("{}");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (cfg) setThresholds(JSON.stringify(cfg.confidence_thresholds || {}, null, 2));
  }, [cfg]);

  async function save() {
    setMsg("");
    try {
      await api.put("/api/admin/config", { confidence_thresholds: JSON.parse(thresholds) });
      setMsg("Saved.");
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  return (
    <div>
      <div className="page-head">
        <h1>Administration</h1>
        <div className="muted">AI model configuration, confidence thresholds, and prompt versions.</div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>Effective AI configuration</h2>
          {cfg && (
            <table>
              <tbody>
                <tr><td className="muted">LLM provider</td><td>{cfg.defaults.ai_provider}</td></tr>
                <tr><td className="muted">Embedding provider</td><td>{cfg.defaults.embedding_provider}</td></tr>
                <tr><td className="muted">deepagents ingestion</td><td>{String(cfg.defaults.ingestion_use_deepagents)}</td></tr>
                <tr><td className="muted">Classify threshold</td><td>{cfg.defaults.classify_confidence_threshold}</td></tr>
                <tr><td className="muted">Extract auto-accept</td><td>{cfg.defaults.extract_autoaccept_threshold}</td></tr>
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h2>Per-field confidence thresholds (JSON)</h2>
          <textarea value={thresholds} onChange={(e) => setThresholds(e.target.value)} rows={8}
            style={{ fontFamily: "monospace" }} />
          <div className="row" style={{ marginTop: 10 }}>
            <button onClick={save}>Save thresholds</button>
            {msg && <span className="muted">{msg}</span>}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Prompt versions</h2>
        <table>
          <thead><tr><th>Key</th><th>Version</th><th>Active</th></tr></thead>
          <tbody>
            {(prompts || []).map((p: any) => (
              <tr key={p.id}><td>{p.key}</td><td>v{p.version}</td><td>{String(p.is_active)}</td></tr>
            ))}
            {(prompts || []).length === 0 && (
              <tr><td colSpan={3} className="muted">Using built-in default prompts (classify@v1, summarize@v1, extract@v1, chat@v1).</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
