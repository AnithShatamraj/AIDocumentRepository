import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";

function Tile({ label, value }: { label: string; value: any }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

export function Dashboard() {
  const { data, isLoading } = useQuery({ queryKey: ["dashboard"], queryFn: () => api.get("/api/dashboard") });
  if (isLoading || !data) return <div className="muted">Loading dashboard…</div>;

  const auto7 = data.extraction_auto_accept_rate?.["7d"];
  return (
    <div>
      <div className="page-head">
        <h1>Dashboard</h1>
        <div className="muted">Operational insight across your authorized documents.</div>
      </div>

      <div className="grid cols-4">
        <Tile label="Total Documents" value={data.total_documents} />
        <Tile
          label="Pending Reviews"
          value={
            <span>
              {data.pending_reviews.count}
              {data.pending_reviews.oldest_age_hours != null && (
                <span className="muted" style={{ fontSize: 13 }}>
                  {" "}· oldest {data.pending_reviews.oldest_age_hours}h
                </span>
              )}
            </span>
          }
        />
        <Tile label="Auto-accept (7d)" value={auto7 == null ? "—" : `${Math.round(auto7 * 100)}%`} />
        <Tile label="AI Cost (MTD)" value={`$${data.ai_cost_period_to_date_usd.toFixed(2)}`} />
      </div>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="card">
          <h2>Processing Status</h2>
          <table>
            <tbody>
              {Object.entries(data.processing_status || {}).map(([k, v]) => (
                <tr key={k}>
                  <td>
                    <StatusBadge status={k} />
                  </td>
                  <td style={{ textAlign: "right" }}>{v as number}</td>
                </tr>
              ))}
              {Object.keys(data.processing_status || {}).length === 0 && (
                <tr><td className="muted">No documents yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>Documents by Category</h2>
          <table>
            <tbody>
              {Object.entries(data.documents_by_category || {}).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td style={{ textAlign: "right" }}>{v as number}</td>
                </tr>
              ))}
              {Object.keys(data.documents_by_category || {}).length === 0 && (
                <tr><td className="muted">No categorized documents yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Recent Uploads</h2>
        <table>
          <thead>
            <tr><th>Name</th><th>Status</th><th>Uploaded</th></tr>
          </thead>
          <tbody>
            {(data.recent_uploads || []).map((d: any) => (
              <tr key={d.id}>
                <td><Link to={`/documents/${d.id}`}>{d.name}</Link></td>
                <td><StatusBadge status={d.status} /></td>
                <td className="muted">{new Date(d.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {(data.recent_uploads || []).length === 0 && (
              <tr><td className="muted">Upload a document to get started.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
