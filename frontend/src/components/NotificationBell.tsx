import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.get("/api/notifications"),
    refetchInterval: 15000,
  });
  const unread = data?.unread || 0;

  async function markAll() {
    await api.post("/api/notifications/read-all");
    qc.invalidateQueries({ queryKey: ["notifications"] });
  }

  return (
    <div style={{ position: "relative" }}>
      <button className="ghost" onClick={() => setOpen((o) => !o)}>
        🔔 {unread > 0 && <span className="pill">{unread}</span>}
      </button>
      {open && (
        <div
          className="card"
          style={{ position: "absolute", right: 0, top: 40, width: 340, zIndex: 20, maxHeight: 420, overflow: "auto" }}
        >
          <div className="spread" style={{ marginBottom: 8 }}>
            <strong>Notifications</strong>
            <button className="ghost" onClick={markAll}>
              Mark all read
            </button>
          </div>
          {(data?.items || []).length === 0 && <div className="muted">Nothing yet.</div>}
          {(data?.items || []).map((n: any) => (
            <div key={n.id} className="stack" style={{ padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
              <div className="spread">
                <strong style={{ fontSize: 13 }}>{n.title}</strong>
                {!n.is_read && <span className="dot running" />}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {n.body}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
