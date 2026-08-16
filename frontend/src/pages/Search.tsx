import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { TypePicker } from "../components/TypePicker";
import { FieldPicker, SearchableField } from "../components/FieldPicker";

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
          Structured (extracted fields)
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

// Which operators make sense for a field's data type, and how to label them.
const OP_SETS: Record<string, { value: string; label: string }[]> = {
  string: [
    { value: "contains", label: "contains" },
    { value: "eq", label: "equals" },
  ],
  number: [
    { value: "eq", label: "equals" },
    { value: "gt", label: "greater than" },
    { value: "gte", label: "greater or equal" },
    { value: "lt", label: "less than" },
    { value: "lte", label: "less or equal" },
  ],
  date: [
    { value: "eq", label: "on" },
    { value: "before", label: "before" },
    { value: "after", label: "after" },
    { value: "between", label: "between" },
  ],
  other: [{ value: "contains", label: "is" }],
};

function opsFor(dataType: string | undefined) {
  if (!dataType) return OP_SETS.string;
  if (dataType === "number" || dataType === "integer" || dataType === "currency") return OP_SETS.number;
  if (dataType === "date" || dataType === "datetime") return OP_SETS.date;
  if (dataType === "string") return OP_SETS.string;
  return OP_SETS.other;
}

function isDateOp(op: string) {
  return op === "before" || op === "after" || op === "between";
}

/** Sibling values from the same list item / object, minus the matched field
 * itself — lets a hit like "Organization: Globex" show its Designation and
 * dates inline without opening the document. */
function contextSummary(row: any): string | null {
  if (!row.context) return null;
  const entries = Object.entries(row.context).filter(
    ([k, v]) => k !== row.field_key && v !== null && v !== undefined && v !== ""
  );
  if (!entries.length) return null;
  return entries
    .map(([k, v]) => `${String(k).replace(/_/g, " ")}: ${v}`)
    .join(" · ");
}

function Structured() {
  const [documentTypeId, setDocumentTypeId] = useState("auto");
  const [field, setField] = useState<SearchableField | null>(null);
  const [op, setOp] = useState("contains");
  const [value, setValue] = useState("");
  const [value2, setValue2] = useState("");
  const [verifiedOnly, setVerifiedOnly] = useState(false);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  const { data: types } = useQuery({
    queryKey: ["document-types"],
    queryFn: () => api.get("/api/document-types"),
  });

  const fieldsQueryKey = documentTypeId === "auto" ? "" : documentTypeId;
  const { data: fields } = useQuery<SearchableField[]>({
    queryKey: ["search-fields", fieldsQueryKey],
    queryFn: () =>
      api.get(`/api/search/fields${fieldsQueryKey ? `?document_type_id=${fieldsQueryKey}` : ""}`),
  });

  // Changing the type scope invalidates the current field pick (it may not
  // exist under the new scope) and resets the operator to something valid.
  useEffect(() => {
    setField(null);
  }, [documentTypeId]);

  const ops = useMemo(() => opsFor(field?.data_type), [field]);
  useEffect(() => {
    if (!ops.find((o) => o.value === op)) setOp(ops[0].value);
  }, [ops]); // eslint-disable-line react-hooks/exhaustive-deps

  const valueInputType = field && isDateOp(op) ? "date" : field && opsFor(field.data_type) === OP_SETS.number ? "number" : "text";

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!field) return;
    setBusy(true);
    try {
      setRes(
        await api.post("/api/search/structured", {
          field_key: field.field_key,
          field_path: field.path_pattern,
          op,
          value: value || null,
          value2: op === "between" ? value2 || null : null,
          document_type_id: documentTypeId === "auto" ? null : documentTypeId,
          verified_only: verifiedOnly,
          limit: 100,
        })
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <form className="card" onSubmit={run} style={{ marginBottom: 16 }}>
        <div className="grid cols-2" style={{ marginBottom: 14 }}>
          <div className="field">
            <label>Document type</label>
            <TypePicker
              types={types || []}
              value={documentTypeId}
              onChange={setDocumentTypeId}
              autoLabel="All types"
              autoDescription="Search extracted fields across every document type."
            />
          </div>
          <div className="field">
            <label>Field</label>
            <FieldPicker fields={fields || []} value={field?.path_pattern || null} onChange={setField} />
          </div>
        </div>

        <div className="grid cols-4">
          <div className="field">
            <label>Operator</label>
            <select value={op} onChange={(e) => setOp(e.target.value)} disabled={!field}>
              {ops.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Value</label>
            <input
              type={valueInputType}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              disabled={!field}
              placeholder={op === "contains" ? "e.g. Northwind" : undefined}
            />
          </div>
          {op === "between" && (
            <div className="field">
              <label>And</label>
              <input type="date" value={value2} onChange={(e) => setValue2(e.target.value)} />
            </div>
          )}
          <div className="field" style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
            <label style={{ margin: 0 }}>
              <input type="checkbox" style={{ width: "auto" }} checked={verifiedOnly}
                onChange={(e) => setVerifiedOnly(e.target.checked)} /> Verified only
            </label>
          </div>
        </div>

        {field?.repeats && (
          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            This field can occur more than once per document (e.g. each line item or party) —
            every matching occurrence is returned as its own row below.
          </div>
        )}

        <button style={{ marginTop: 12 }} disabled={!field || busy}>{busy ? "Searching…" : "Search"}</button>
      </form>

      {res && (
        <div className="card">
          <div className="muted" style={{ marginBottom: 10 }}>
            {res.count} result(s){res.verified_only ? " · verified only" : ""}
          </div>
          <table>
            <thead>
              <tr><th>Document</th><th>Field</th><th>Value</th><th>Context</th><th>Status</th><th>Conf.</th></tr>
            </thead>
            <tbody>
              {res.results.map((r: any, i: number) => (
                <tr key={i}>
                  <td><Link to={`/documents/${r.document_id}`}>{r.document_name}</Link></td>
                  <td className="muted" style={{ fontSize: 12 }}>{r.field_path}</td>
                  <td>{r.raw_value}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{contextSummary(r) || "—"}</td>
                  <td className="muted">{r.review_status}</td>
                  <td className="muted conf">{Math.round((r.confidence || 0) * 100)}%</td>
                </tr>
              ))}
              {res.results.length === 0 && (
                <tr><td colSpan={6} className="muted">No matches.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
