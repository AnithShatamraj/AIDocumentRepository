import { ReactNode, useState } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { NotificationBell } from "./NotificationBell";

const links = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/documents", label: "Documents" },
  { to: "/search", label: "Search" },
  { to: "/research", label: "Research Assistant" },
  { to: "/review", label: "Review Queue" },
  { to: "/categories", label: "Categories" },
  { to: "/admin", label: "Administration" },
];

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const [navCollapsed, setNavCollapsed] = useState(
    () => localStorage.getItem("aidocs_nav_collapsed") === "1"
  );
  const [theme, setTheme] = useState(
    () => localStorage.getItem("aidocs_theme") || "dark"
  );

  function toggleNav() {
    setNavCollapsed((c) => {
      localStorage.setItem("aidocs_nav_collapsed", c ? "0" : "1");
      return !c;
    });
  }

  function toggleTheme() {
    setTheme((t) => {
      const next = t === "dark" ? "light" : "dark";
      localStorage.setItem("aidocs_theme", next);
      document.documentElement.dataset.theme = next;
      return next;
    });
  }

  return (
    <div className="app">
      <aside className={`sidebar${navCollapsed ? " collapsed" : ""}`}>
        <div className="brand">AI<span>Docs</span> Repository</div>
        <nav className="nav">
          {links.map((l) => (
            <NavLink key={l.to} to={l.to} end={l.end}>
              {l.label}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot muted" style={{ marginTop: "auto", fontSize: 12 }}>
          Permission-aware · multi-tenant
        </div>
      </aside>
      <div className="main">
        <div className="topbar">
          <div className="row">
            <button className="ghost" onClick={toggleNav} title="Toggle navigation">
              ☰
            </button>
            <span className="muted">Tenant workspace</span>
          </div>
          <div className="row">
            <button
              className="ghost"
              onClick={toggleTheme}
              title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            >
              {theme === "dark" ? "☀️" : "🌙"}
            </button>
            <NotificationBell />
            <span className="pill">{user?.role}</span>
            <span className="muted">{user?.email}</span>
            <button className="ghost" onClick={logout}>
              Sign out
            </button>
          </div>
        </div>
        <div className="content">{children}</div>
      </div>
    </div>
  );
}
