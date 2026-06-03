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
      <header className="chrome-bar sticky top-0 z-20">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 pt-[max(0.75rem,env(safe-area-inset-top))] pb-3">
          <Link to="/" className="group flex items-center gap-2">
            <span
              aria-hidden
              className="inline-block h-2.5 w-2.5 rounded-full bg-accent-gradient shadow-glow-accent transition-transform group-hover:scale-110"
            />
            <span className="brand-mark text-base font-semibold tracking-tight">
              agentic&#8209;rag
            </span>
          </Link>
          <button
            onClick={logout}
            className="text-xs text-muted transition-colors hover:text-danger"
          >
            log out
          </button>
        </div>
      </header>
      <main>{children}</main>
    </div>
  );
}
