import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

export function Categories() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [err, setErr] = useState("");

  const { data } = useQuery({ queryKey: ["categories"], queryFn: () => api.get("/api/categories") });

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    try {
      await api.post("/api/categories", { name, description: desc });
      setName("");
      setDesc("");
      qc.invalidateQueries({ queryKey: ["categories"] });
    } catch (e: any) {
      setErr(e.message);
    }
  }

  async function toggle(c: any) {
    await api.patch(`/api/categories/${c.id}`, { is_enabled: !c.is_enabled });
    qc.invalidateQueries({ queryKey: ["categories"] });
  }

  return (
    <div>
      <div className="page-head">
        <h1>Categories</h1>
        <div className="muted">Each category binds a versioned extraction schema (stored as data).</div>
      </div>

      <div className="split">
        <div className="card">
          <table>
            <thead>
              <tr><th>Name</th><th>Description</th><th>State</th><th></th></tr>
            </thead>
            <tbody>
              {(data || []).map((c: any) => (
                <tr key={c.id}>
                  <td><strong>{c.name}</strong></td>
                  <td className="muted">{c.description}</td>
                  <td>{c.is_enabled ? <span className="ok">enabled</span> : <span className="muted">disabled</span>}</td>
                  <td><button className="ghost" onClick={() => toggle(c)}>{c.is_enabled ? "Disable" : "Enable"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>New category</h2>
          <form onSubmit={create}>
            <div className="field">
              <label>Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} required />
            </div>
            <div className="field">
              <label>Description</label>
              <textarea value={desc} onChange={(e) => setDesc(e.target.value)} rows={3} />
            </div>
            {err && <div className="err" style={{ marginBottom: 10 }}>{err}</div>}
            <button>Create category</button>
            <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
              Admin only. Add extraction fields via the API/schema endpoint.
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
