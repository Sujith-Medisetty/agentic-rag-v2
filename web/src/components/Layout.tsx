import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { authApi } from "@/lib/api";
import { clearToken } from "@/lib/auth";
import { useTheme } from "@/lib/theme";

// Top chrome shown above non-chat pages. ChatPage renders full-screen so it
// has its own header. Designed mobile-first: compact on phones, generous on
// tablet+ (max-w-5xl). All controls hit ≥44pt targets.
//
// No custom PWA install UI here on purpose — see comment in App.tsx.
// Users install via the browser's native flow (URL bar install icon
// on Chrome/Edge, Share → Add to Home Screen on iOS).
export default function Layout({ children }: { children: React.ReactNode }) {
  const { effective, toggle } = useTheme();

  // "New version available" toast. The SW registration code in
  // src/pwa/registerSW.ts dispatches `ojas:sw-update` when a new service
  // worker is waiting to take over. We show a fixed toast at the bottom
  // of the screen with a refresh button — clicking it dispatches
  // `ojas:sw-apply` which the SW handler picks up and skipWaits.
  const [swUpdate, setSwUpdate] = useState(false);
  useEffect(() => {
    const onUpdate = () => setSwUpdate(true);
    window.addEventListener("ojas:sw-update", onUpdate);
    return () => window.removeEventListener("ojas:sw-update", onUpdate);
  }, []);

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

      {/* Service worker update toast. Shows at the bottom-center when a
          new Ojas version is waiting to take over. User clicks the button
          to apply (skipWaiting + reload). Auto-applied would surprise the
          user mid-typing — better to make it explicit. */}
      {swUpdate && (
        <div
          role="status"
          aria-live="polite"
          className="fixed inset-x-0 bottom-4 z-50 mx-auto flex w-fit max-w-[calc(100vw-2rem)] items-center gap-3 rounded-lg border border-info/40 bg-info/10 px-4 py-2.5 text-sm text-text shadow-lg backdrop-blur"
        >
          <span aria-hidden="true" className="text-base">✨</span>
          <span className="font-medium">A new version of Ojas is ready.</span>
          <button
            type="button"
            onClick={() => {
              // Tell the SW to skipWaiting; the `controlling` listener in
              // registerSW.ts will then reload the page.
              window.dispatchEvent(new CustomEvent("ojas:sw-apply"));
              setSwUpdate(false);
            }}
            className="ml-1 rounded-md bg-accent px-3 py-1 text-xs font-semibold text-bg hover:bg-accent/90"
          >
            Refresh
          </button>
          <button
            type="button"
            onClick={() => setSwUpdate(false)}
            className="rounded-md px-2 py-1 text-xs text-muted hover:text-text"
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      )}
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
function DownloadIcon() {
  return (
    <svg
      width="14" height="14" viewBox="0 0 24 24"
      fill="none" stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M12 4v12M6 12l6 6 6-6M5 20h14" />
    </svg>
  );
}
