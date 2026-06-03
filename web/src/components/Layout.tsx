import { Link } from "react-router-dom";
import { authApi } from "@/lib/api";
import { clearToken } from "@/lib/auth";

// Thin top bar shown above non-chat pages. ChatPage has its own header so it
// can show the WebSocket status pill — there we render Outlet content full-screen.
export default function Layout({ children }: { children: React.ReactNode }) {
  const logout = async () => {
    try {
      await authApi.logout();
    } catch {
      /* token will be cleared either way */
    }
    clearToken();
    window.location.href = "/";
  };

  return (
    <div className="min-h-screen">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <Link to="/" className="font-semibold tracking-tight">
            agentic-rag
          </Link>
          <button
            onClick={logout}
            className="text-xs text-muted hover:text-danger"
          >
            log out
          </button>
        </div>
      </header>
      <main>{children}</main>
    </div>
  );
}
