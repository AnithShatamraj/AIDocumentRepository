import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { Documents } from "./pages/Documents";
import { DocumentDetail } from "./pages/DocumentDetail";
import { Search } from "./pages/Search";
import { Research } from "./pages/Research";
import { Review } from "./pages/Review";
import { Categories } from "./pages/Categories";
import { Admin } from "./pages/Admin";

export function App() {
  const { user, loading } = useAuth();

  if (loading) return <div className="login-wrap muted">Loading…</div>;
  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/documents" element={<Documents />} />
        <Route path="/documents/:id" element={<DocumentDetail />} />
        <Route path="/search" element={<Search />} />
        <Route path="/research" element={<Research />} />
        <Route path="/review" element={<Review />} />
        <Route path="/categories" element={<Categories />} />
        <Route path="/admin" element={<Admin />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
