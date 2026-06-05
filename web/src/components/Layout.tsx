import { Link } from "react-router-dom";
import { authApi } from "@/lib/api";
import { clearToken } from "@/lib/auth";
import { useTheme } from "@/lib/theme";

// Top chrome shown above non-chat pages. ChatPage renders full-screen so it
// has its own header. Designed mobile-first: compact on phones, generous on
// tablet+ (max-w-5xl). All controls hit ≥44pt targets.
export default function Layout({ children }: { children: React.ReactNode }) {
  const { effective, toggle } = useTheme();

  const logout = async () => {
    try { await authApi.logout(); } catch { /* clear either way */ }
    clearToken();
    window.location.href = "/";
  };

  return (
    <div className="min-h-screen">
      <header className="chrome-bar sticky top-0 z-20">
        <div
          className="mx-auto flex max-w-5xl items-center gap-3 px-4
                     pt-[max(0.6rem,env(safe-area-inset-top))] pb-2.5
                     sm:px-6 sm:pt-3 sm:pb-3"
        >
          <Link to="/" className="group flex min-w-0 flex-1 items-center gap-2">
            <span
              aria-hidden
              className="inline-block h-2.5 w-2.5 rounded-full bg-accent-gradient transition-transform group-hover:scale-110"
              style={{ boxShadow: "0 0 0 4px hsl(var(--accent) / 0.18)" }}
            />
            <span className="brand-mark truncate text-base font-semibold tracking-tight">
              Ojas
            </span>
          </Link>
          <button
            onClick={toggle}
            className="btn-icon"
            title={`Switch to ${effective === "dark" ? "light" : "dark"} mode`}
            aria-label={`Switch to ${effective === "dark" ? "light" : "dark"} mode`}
          >
            {effective === "dark" ? <SunIcon /> : <MoonIcon />}
          </button>
          <button
            onClick={logout}
            className="hidden text-xs text-muted transition-colors hover:text-danger sm:inline"
          >
            log out
          </button>
          <button
            onClick={logout}
            className="btn-icon sm:hidden"
            title="Log out"
            aria-label="Log out"
          >
            <LogoutIcon />
          </button>
        </div>
      </header>
      <main>{children}</main>
    </div>
  );
}

// Inline icons (no extra deps). Sized to match btn-icon's 36×36 frame
// (16×16 inside a 9×9 padding). Stroke uses currentColor so they pick up
// `text-muted` / `text-text` from the parent.
function SunIcon() {
  return (
    <svg
      width="16" height="16" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      width="16" height="16" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg
      width="16" height="16" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden
    >
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
    </svg>
  );
}
