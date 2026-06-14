import { Link } from "react-router-dom";
import { authApi } from "@/lib/api";
import { clearToken } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import InstallButton from "@/components/InstallButton";
import { SunIcon, MoonIcon, LogoutIcon } from "@/components/icons";

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
          {/* Install Ojas as PWA — renders nothing once already installed. */}
          <InstallButton variant="ghost" />
          <button
            onClick={toggle}
            className="btn-icon"
            title={`Switch to ${effective === "dark" ? "light" : "dark"} mode`}
            aria-label={`Switch to ${effective === "dark" ? "light" : "dark"} mode`}
          >
            {effective === "dark" ? <SunIcon className="h-4 w-4" /> : <MoonIcon className="h-4 w-4" />}
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
            <LogoutIcon className="h-4 w-4" />
          </button>
        </div>
      </header>
      <main>{children}</main>
    </div>
  );
}
