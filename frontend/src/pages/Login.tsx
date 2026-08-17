import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [tenantSlug, setTenantSlug] = useState("acme-corp");
  const [email, setEmail] = useState("admin@acme.com");
  const [password, setPassword] = useState("admin12345");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(tenantSlug, email, password);
      nav("/");
    } catch (err: any) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand" style={{ padding: "0 0 16px" }}>
          AI<span>Docs</span> Repository
        </div>
        <div className="field">
          <label>Workspace</label>
          <input value={tenantSlug} onChange={(e) => setTenantSlug(e.target.value)} required />
        </div>
        <div className="field">
          <label>Email</label>
          <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" required />
        </div>
        <div className="field">
          <label>Password</label>
          <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" required />
        </div>
        {error && <div className="err" style={{ marginBottom: 12 }}>{error}</div>}
        <button style={{ width: "100%" }} disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <div className="muted" style={{ marginTop: 12, fontSize: 12 }}>
          Seeded admin: workspace acme-corp · admin@acme.com / admin12345
        </div>
      </form>
    </div>
  );
}
