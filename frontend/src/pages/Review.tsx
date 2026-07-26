import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";

export function Review() {
  const qc = useQueryClient();
  const [sort, setSort] = useState("age");
  const [selected, setSelected] = useState<any>(null);
  const [corrected, setCorrected] = useState("");
  const [catIds, setCatIds] = useState<string[]>([]);

  const { data: items } = useQuery({
    queryKey: ["review", sort],
    queryFn: () => api.get(`/api/review?sort=${sort}`),
    refetchInterval: 6000,
  });
  const { data: categories } = useQuery({ queryKey: ["categories"], queryFn: () => api.get("/api/categories") });

  async function select(item: any) {
    setSelected(item);
    setCorrected(item.payload?.raw_value || "");
    setCatIds([]);
    try {
      await api.post(`/api/review/${item.id}/claim`);
    } catch {
      /* already claimed — still viewable */
    }
  }

  async function resolve(action: string) {
    if (!selected) return;
    await api.post(`/api/review/${selected.id}/resolve`, {
      action,
      corrected_value: action === "modify" ? corrected : null,
      category_ids: selected.kind === "classification" ? catIds : null,
    });
    setSelected(null);
    qc.invalidateQueries({ queryKey: ["review"] });
  }

  return (
    <div>
      <div className="page-head spread">
        <div>
          <h1>Review Queue</h1>
          <div className="muted">Human validation for low-confidence AI results.</div>
        </div>
        <select value={sort} onChange={(e) => setSort(e.target.value)} style={{ width: 200 }}>
          <option value="age">Oldest first</option>
          <option value="confidence">Lowest confidence first</option>
        </select>
      </div>

      <div className="split">
        <div className="card">
          <table>
            <thead>
              <tr><th>Document</th><th>Kind</th><th>Field</th><th>Conf.</th></tr>
            </thead>
            <tbody>
              {(items || []).map((it: any) => (
                <tr key={it.id} onClick={() => select(it)} style={{ cursor: "pointer" }}>
                  <td><Link to={`/documents/${it.document_id}`} onClick={(e) => e.stopPropagation()}>doc</Link></td>
                  <td>{it.kind}</td>
                  <td>{it.field_name || "—"}</td>
                  <td className="muted conf">{Math.round((it.confidence || 0) * 100)}%</td>
                </tr>
              ))}
              {items && items.length === 0 && <tr><td colSpan={4} className="muted">Queue is clear 🎉</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="card">
          {!selected && <div className="muted">Select an item to review.</div>}
          {selected && (
            <div className="stack">
              <div className="spread">
                <h2 style={{ margin: 0 }}>{selected.kind === "extraction" ? selected.field_name : "Categorization"}</h2>
                <Link to={`/documents/${selected.document_id}`}>Open document ↗</Link>
              </div>

              {selected.kind === "extraction" ? (
                <>
                  <div className="field">
                    <label>AI extracted value ({Math.round((selected.confidence || 0) * 100)}% confidence)</label>
                    <input value={corrected} onChange={(e) => setCorrected(e.target.value)} />
                  </div>
                  <div className="field">
                    <label>Source context</label>
                    <div className="card muted" style={{ fontSize: 13 }}>
                      {selected.payload?.raw_value || "—"}
                    </div>
                  </div>
                  <div className="row">
                    <button onClick={() => resolve("accept")}>Accept</button>
                    <button className="secondary" onClick={() => resolve("modify")}>Save correction</button>
                    <button className="ghost" onClick={() => resolve("reject")}>Reject</button>
                  </div>
                </>
              ) : (
                <>
                  <div className="muted">This document scored below the classification threshold. Assign categories:</div>
                  <div className="stack">
                    {(categories || []).map((c: any) => (
                      <label key={c.id} className="row" style={{ margin: 0 }}>
                        <input
                          type="checkbox"
                          style={{ width: "auto" }}
                          checked={catIds.includes(c.id)}
                          onChange={(e) =>
                            setCatIds((prev) => (e.target.checked ? [...prev, c.id] : prev.filter((x) => x !== c.id)))
                          }
                        />
                        {c.name}
                      </label>
                    ))}
                  </div>
                  <div className="row">
                    <button disabled={catIds.length === 0} onClick={() => resolve("accept")}>Assign & extract</button>
                    <button className="ghost" onClick={() => resolve("reject")}>Leave uncategorized</button>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
