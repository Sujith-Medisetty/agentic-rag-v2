// Workspace — Claude-desktop-style shell that wraps the chat with a left
// sidebar. The sidebar holds every session in the default project, a "+ New
// chat" button, theme toggle, and log-out. There's NO project-picker page
// anymore for casual use: when the user logs in, this page auto-creates a
// default project at ~/Desktop/Forge and lands them directly in the chat.

import { useEffect, useState } from "react";
import { Outlet, useNavigate, useParams, Link } from "react-router-dom";
import { projectsApi, sessionsApi, sessionApi, authApi, ApiError } from "@/lib/api";
import { clearToken } from "@/lib/auth";
import type { Project, Session } from "@/lib/types";

export default function Workspace() {
  const navigate = useNavigate();
  const { sessionId: activeSessionId } = useParams<{ sessionId?: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(() => {
    // Open by default on desktop, closed on mobile (saves real estate).
    if (typeof window === "undefined") return true;
    return window.matchMedia("(min-width: 768px)").matches;
  });


  // Bootstrap: resolve default project + its sessions.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const p = await projectsApi.getDefault();
        if (cancelled) return;
        setProject(p);
        const ss = await sessionsApi.list(p.id);
        if (cancelled) return;
        setSessions(ss);
      } catch (e) {
        if (!cancelled) setLoadErr(e instanceof ApiError ? e.message : "failed to load workspace");
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const startNew = async () => {
    if (!project) return;
    try {
      const s = await sessionsApi.create(
        project.id,
        `Chat ${new Date().toLocaleString()}`,
      );
      setSessions((prev) => [s, ...prev]);
      navigate(`/s/${s.id}`);
      // On mobile auto-close the sidebar after picking a session.
      if (!window.matchMedia("(min-width: 768px)").matches) setSidebarOpen(false);
    } catch (e: any) {
      setLoadErr(e?.message ?? "failed to create chat");
    }
  };

  const openSession = (sid: string) => {
    navigate(`/s/${sid}`);
    if (!window.matchMedia("(min-width: 768px)").matches) setSidebarOpen(false);
  };

  const deleteSession = async (sid: string) => {
    if (!confirm("Delete this chat?\n\nIts messages, plan, and event log will be permanently removed. Workspace files on disk are NOT touched.")) return;
    try {
      await sessionApi.cancel(sid).catch(() => {});
      await sessionsApi.remove(sid);
      setSessions((prev) => prev.filter((s) => s.id !== sid));
      if (activeSessionId === sid) navigate("/");
    } catch (e: any) {
      setLoadErr(e?.message ?? "delete failed");
    }
  };

  const logout = async () => {
    try { await authApi.logout(); } catch { /* clear either way */ }
    clearToken();
    window.location.href = "/";
  };

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Mobile backdrop — taps close the sidebar. */}
      {sidebarOpen && (
        <button
          aria-label="Close sidebar"
          onClick={() => setSidebarOpen(false)}
          className="fixed inset-0 z-20 bg-black/40 backdrop-blur-sm md:hidden"
        />
      )}

      {/* Sidebar — three states:
            • Mobile, open:   fixed overlay sliding in from left + backdrop
            • Mobile, closed: fixed but translated off-screen
            • Desktop, open:  normal flex column in the page layout
            • Desktop, closed: rendered as `display: none` so it takes zero
              space and main column gets the full width
          Each viewport gets its OWN set of positioning classes — no
          conflicting overrides between mobile and desktop variants.
       */}
      <aside
        className={
          sidebarOpen
            ? "fixed inset-y-0 left-0 z-30 flex w-72 flex-col border-r border-border bg-surface transition-transform duration-200 translate-x-0 md:static md:z-0"
            : "fixed inset-y-0 left-0 z-30 flex w-72 flex-col border-r border-border bg-surface transition-transform duration-200 -translate-x-full md:hidden"
        }
      >
        {/* Header — brand + collapse */}
        <div className="flex items-center gap-2 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-3">
          <Link to="/" className="group flex min-w-0 flex-1 items-center gap-2">
            <span
              aria-hidden
              className="inline-block h-2.5 w-2.5 rounded-full bg-accent-gradient transition-transform group-hover:scale-110"
              style={{ boxShadow: "0 0 0 4px hsl(var(--accent) / 0.18)" }}
            />
            <span className="brand-mark truncate text-base font-semibold tracking-tight">
              Forge
            </span>
          </Link>
          <button
            onClick={() => setSidebarOpen(false)}
            className="btn-icon"
            title="Collapse sidebar"
            aria-label="Collapse sidebar"
          >
            <ChevronLeftIcon />
          </button>
        </div>

        {/* New chat — full-width accent-tinted button so it fills the
            sidebar's content area. Clear primary action, easy tap target. */}
        <div className="px-3 pb-2">
          <button
            type="button"
            onClick={startNew}
            disabled={!project}
            className="flex w-full items-center justify-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-sm font-medium text-accent transition-colors hover:bg-accent/15 hover:border-accent/60 disabled:opacity-50"
          >
            <PlusIcon />
            <span>New chat</span>
          </button>
        </div>

        {/* Sessions list — clean single-line rows under a small "Recents"
            label so the section reads at a glance, Claude-style. */}
        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {loadErr && (
            <div className="mx-1 mb-2 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-tx-xs text-danger">
              {loadErr}
            </div>
          )}
          {sessions.length > 0 && (
            <div className="px-3 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-subtle">
              Recents
            </div>
          )}
          {sessions.length === 0 ? (
            <div className="px-3 py-6 text-center text-tx-xs text-subtle">
              No chats yet. Tap “+ New chat” above to start.
            </div>
          ) : (
            <ul className="space-y-0.5">
              {sessions.map((s) => {
                const isActive = s.id === activeSessionId;
                return (
                  <li key={s.id} className="group relative">
                    <button
                      type="button"
                      onClick={() => openSession(s.id)}
                      className={`
                        block w-full truncate rounded-md px-3 py-1.5 pr-9 text-left text-sm transition-colors
                        ${isActive
                          ? "bg-accent/10 text-text"
                          : "text-muted hover:bg-elevated hover:text-text"}
                      `}
                      title={s.name}
                    >
                      {s.name}
                    </button>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteSession(s.id);
                      }}
                      className="absolute right-1.5 top-1/2 hidden h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-subtle hover:bg-danger/10 hover:text-danger group-hover:flex"
                      title="Delete chat"
                      aria-label="Delete chat"
                    >
                      <TrashIcon />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Footer — workspace path + a clean Log out button. The theme
            toggle is intentionally NOT here; one source of truth lives in
            the app header (Layout) / chat header so there's no duplication. */}
        <div className="border-t border-border px-3 py-3">
          {project && (
            <div
              className="mb-2.5 truncate font-mono text-[10px] text-subtle"
              title={project.workspace_path}
            >
              {project.workspace_path}
            </div>
          )}
          <button
            onClick={logout}
            className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-danger/30 bg-danger/10 px-3 py-1.5 text-sm font-medium text-danger transition-colors hover:border-danger/50 hover:bg-danger/15"
          >
            <LogoutIcon />
            <span>Log out</span>
          </button>
        </div>
      </aside>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Floating sidebar-open button when collapsed. */}
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            className="fixed left-3 top-[max(0.75rem,env(safe-area-inset-top))] z-20 btn-icon shadow-soft"
            title="Open sidebar"
            aria-label="Open sidebar"
          >
            <MenuIcon />
          </button>
        )}

        {/* Either: active chat (via Outlet → ChatPage), welcome screen with
            CTA, or a loading state while the default project resolves. */}
        {project ? (
          activeSessionId ? (
            <Outlet context={{ project, sidebarOpen }} />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center px-6 py-8 text-center">
              <div
                aria-hidden
                className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-gradient shadow-lift"
              >
                <span className="text-2xl font-bold text-white">F</span>
              </div>
              <h1 className="font-serif text-3xl font-semibold tracking-tight">
                Welcome to Forge
              </h1>
              <p className="mt-2 max-w-md text-sm text-muted">
                Pick a chat from the sidebar to resume it, or start a new one.
                Everything you build lands under <span className="font-mono">~/Desktop/Forge</span>.
              </p>
              <button
                type="button"
                onClick={startNew}
                className="btn-primary mt-5"
              >
                + New chat
              </button>
            </div>
          )
        ) : (
          <div className="flex flex-1 items-center justify-center px-6 py-8 text-center">
            <div>
              <div className="font-serif text-2xl font-semibold tracking-tight">
                {loadErr ? "Couldn't load workspace" : "Setting up your workspace…"}
              </div>
              <p className="mt-2 text-sm text-muted">
                {loadErr ?? "Creating ~/Desktop/Forge if it doesn't exist."}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Icons ──────────────────────────────────────────────────────────────────
function MenuIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 6h18M3 12h18M3 18h18" />
    </svg>
  );
}
function ChevronLeftIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M15 18l-6-6 6-6" />
    </svg>
  );
}
function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6" />
    </svg>
  );
}
function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}
function LogoutIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
    </svg>
  );
}

// Compact relative time: "just now", "12m", "3h", "yesterday", "Mon", "Mar 4".
// Used in the sidebar session rows so the user can scan "was that last hour
// or last week?" without doing math.
function formatRelativeTime(secs: number): string {
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - secs));
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  const days = Math.floor(diff / 86400);
  if (days === 1) return "yesterday";
  if (days < 7) {
    return new Date(secs * 1000).toLocaleDateString(undefined, { weekday: "short" });
  }
  return new Date(secs * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
