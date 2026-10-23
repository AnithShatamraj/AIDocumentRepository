import { createContext, ReactNode, useContext, useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { NotificationBell } from "./NotificationBell";

const links = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/documents", label: "Documents" },
  { to: "/research", label: "Research" },
];

const adminLinks = [
  { to: "/admin", label: "Settings" },
  { to: "/document-types", label: "Document Types" },
];

interface LayoutNavCtx {
  navCollapsed: boolean;
  setNavCollapsed: (v: boolean) => void;
  /** Hides the topbar entirely and tightens the content area's padding --
   * the rest of "Focus mode"'s extra vertical height, beyond the nav
   * sidebar. Exit Focus in the page itself is always the way back. */
  chromeHidden: boolean;
  setChromeHidden: (v: boolean) => void;
}
const LayoutContext = createContext<LayoutNavCtx | null>(null);

/** Lets a page (e.g. a document "Focus mode") collapse/restore the nav
 * sidebar from outside Layout, the same way the topbar's own toggle does. */
export function useLayoutNav() {
  const ctx = useContext(LayoutContext);
  if (!ctx) throw new Error("useLayoutNav must be used within Layout");
  return ctx;
}

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const [navCollapsed, setNavCollapsedState] = useState(
    () => localStorage.getItem("aidocs_nav_collapsed") === "1"
  );
  const [chromeHidden, setChromeHidden] = useState(false);
  // The Administration group opens by default when you're on one of its pages,
  // so the active item is never hidden; otherwise it stays as the user left it.
  const { pathname } = useLocation();
  const inAdmin = adminLinks.some((l) => pathname === l.to || pathname.startsWith(`${l.to}/`));
  const [adminOpen, setAdminOpen] = useState(inAdmin);
  useEffect(() => {
    if (inAdmin) setAdminOpen(true);
  }, [inAdmin]);
  const [theme, setTheme] = useState(
    () => localStorage.getItem("aidocs_theme") || "dark"
  );

  function setNavCollapsed(v: boolean) {
    setNavCollapsedState(v);
    localStorage.setItem("aidocs_nav_collapsed", v ? "1" : "0");
  }

  function toggleNav() {
    setNavCollapsed(!navCollapsed);
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
    <LayoutContext.Provider value={{ navCollapsed, setNavCollapsed, chromeHidden, setChromeHidden }}>
      <div className="app">
        <aside className={`sidebar${navCollapsed ? " collapsed" : ""}`}>
          <div className="brand">AI<span>Docs</span> Repository</div>
          <nav className="nav">
            {links.map((l) => (
              <NavLink key={l.to} to={l.to} end={l.end}>
                {l.label}
              </NavLink>
            ))}
            <button
              type="button"
              className={`nav-group${inAdmin ? " current" : ""}`}
              aria-expanded={adminOpen}
              onClick={() => setAdminOpen((o) => !o)}
            >
              <span>Administration</span>
              <span className="chev">{adminOpen ? "▾" : "▸"}</span>
            </button>
            {adminOpen && (
              <div className="nav-sub">
                {adminLinks.map((l) => (
                  <NavLink key={l.to} to={l.to}>
                    {l.label}
                  </NavLink>
                ))}
              </div>
            )}
          </nav>
          <div className="side-foot muted" style={{ marginTop: "auto", fontSize: 12 }}>
            Permission-aware · multi-tenant
          </div>
        </aside>
        <div className="main">
          {!chromeHidden && (
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
          )}
          <div className={`content${chromeHidden ? " tight" : ""}`}>{children}</div>
        </div>
      </div>
    </LayoutContext.Provider>
  );
}
