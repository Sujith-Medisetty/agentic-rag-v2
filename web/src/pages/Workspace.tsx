// Workspace — Claude-desktop-style shell that wraps the chat with a left
// sidebar. The sidebar holds every session in the default project, a "+ New
// chat" button, theme toggle, and log-out. There's NO project-picker page
// anymore for casual use: when the user logs in, this page auto-creates a
// default project at the platform-default workspace (Linux: ~/ojas) and
// lands them directly in the chat.

import { useEffect, useRef, useState } from "react";
import { Outlet, useNavigate, useParams, Link } from "react-router-dom";
import {
  projectsApi, sessionsApi, sessionApi, authApi, ApiError,
  type DeleteJobStart, type DeleteJobStatus,
  type AuthUser,
} from "@/lib/api";
import { clearToken } from "@/lib/auth";
import type { Project, Session } from "@/lib/types";
import { useSessions } from "@/lib/sessionContext";
import InstallButton from "@/components/InstallButton";
import DeleteProgressModal from "@/components/DeleteProgressModal";

export default function Workspace() {
  const navigate = useNavigate();
  const { sessionId: activeSessionId } = useParams<{ sessionId?: string }>();
  // Ref always holds the latest activeSessionId so async callbacks don't read
  // a stale closure value (e.g. when startNew() navigates mid-load).
  const activeSessionIdRef = useRef<string | undefined>(activeSessionId);
  activeSessionIdRef.current = activeSessionId;
  const [project, setProject] = useState<Project | null>(null);
  // Sessions live in a shared Context (see lib/sessionContext.tsx) so the
  // chat page can update the sidebar's view of the same list — no prop
  // drilling, no window events, just React. We read from
  // sessionsStore.list(project.id) for the render and call
  // sessionsStore.setAll / add / remove / rename for writes.
  const sessionsStore = useSessions();
  const [me, setMe] = useState<AuthUser | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(() => {
    // Open by default on desktop, closed on mobile (saves real estate).
    if (typeof window === "undefined") return true;
    return window.matchMedia("(min-width: 768px)").matches;
  });

  // Inline rename state for the chat sidebar (mirrors SessionList.tsx).
  // `editingId` is the session currently being renamed; the input value
  // lives in `editingValue`; `editingBusy` disables the input while the
  // PATCH is in flight; the input ref is auto-focused on edit start.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [editingBusy, setEditingBusy] = useState(false);
  // DeleteProgressModal state. The sidebar entry is removed
  // optimistically the moment the modal opens; this state just
  // tracks the in-flight job so the modal can poll for progress.
  const [deletingSession, setDeletingSession] = useState<{
    id: string;
    name: string;
    job: DeleteJobStart | null;
  } | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);
  // Name-conflict modal (real React <dialog>, not a native alert).
  const [conflict, setConflict] = useState<{
    desired: string;
    existingId: string;
    existingName: string;
  } | null>(null);
  useEffect(() => {
    if (editingId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingId]);

  // Bootstrap: resolve default project + its sessions + the logged-in user.
  // The /me lookup is what tells the sidebar whether to show the Admin link.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [p, user] = await Promise.all([
          projectsApi.getDefault(),
          authApi.me().catch(() => null),
        ]);
        if (cancelled) return;
        setProject(p);
        setMe(user);
        const ss = await sessionsApi.list(p.id);
        if (cancelled) return;
        // Merge: if startNew() ran while we were loading, the new session
        // is already in the context (from sessions.add()) but not in
        // `ss` (a stale snapshot). Keep locally-added sessions so they
        // don't vanish from the sidebar.
        sessionsStore.setAll(p.id, (() => {
          const apiIds = new Set(ss.map((s) => s.id));
          const localOnly = sessionsStore.list(p.id).filter(
            (s) => !apiIds.has(s.id),
          );
          return localOnly.length > 0 ? [...localOnly, ...ss] : ss;
        })());
        // Auto-open the most recent session when the user lands with no
        // session selected (e.g. logging in on a new device). Use the ref
        // so we read the *current* URL param, not the stale closure value.
        if (ss.length > 0 && !activeSessionIdRef.current) {
          navigate(`/s/${ss[0].id}`, { replace: true });
        }
      } catch (e) {
        if (!cancelled) setLoadErr(e instanceof ApiError ? e.message : "failed to load workspace");
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startNew = async () => {
    if (!project) return;
    try {
      const s = await sessionsApi.create(
        project.id,
        `Chat ${new Date().toLocaleString()}`,
      );
      sessionsStore.add(project.id, s);
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

  const deleteSession = async (s: Session) => {
    if (!confirm(
      `Delete this chat "${s.name}"?\n\n` +
      `This permanently removes the chat history, the agent's edited files, ` +
      `the sub-project build output, and any live URLs + systemd units for ` +
      `this session. The sidebar entry disappears immediately; the server ` +
      `cleanup runs in the background.\n\nThis cannot be undone.`
    )) return;
    // 1. Cancel any in-flight agent turn (best-effort, fire-and-forget).
    sessionApi.cancel(s.id).catch(() => {});
    // 2. Optimistic sidebar removal. The user shouldn't have to wait
    //    for the server round-trip to see the entry disappear.
    sessionsStore.remove(s.id);
    if (activeSessionIdRef.current === s.id) navigate("/");
    // 3. Kick off the async server-side teardown and open the
    //    progress modal so the user can see what's happening.
    try {
      const job = await sessionApi.startDelete(s.id);
      setDeletingSession({ id: s.id, name: s.name, job });
    } catch (e: any) {
      // Server refused (e.g. 404 because the session was already gone,
      // 409 because a previous delete is still running). The sidebar
      // is already updated; the modal just won't open.
      setLoadErr(e?.message ?? "delete failed to start");
    }
  };

  const beginEdit = (s: Session) => {
    setEditingId(s.id);
    setEditingValue(s.name);
  };
  const cancelEdit = () => {
    setEditingId(null);
    setEditingValue("");
  };
  const commitEdit = async (s: Session) => {
    const trimmed = editingValue.trim();
    if (!trimmed || trimmed === s.name) {
      cancelEdit();
      return;
    }
    setEditingBusy(true);
    try {
      const { session, wasSuffixed, actualName } =
        await sessionsApi.renameWithSufStatus(s.id, trimmed);
      sessionsStore.rename(s.id, session.name);
      if (wasSuffixed && actualName !== trimmed) {
        sessionsStore.setToast({
          message: `Renamed to "${actualName}" — "${trimmed}" was already taken.`,
        });
      }
      cancelEdit();
    } catch (e: any) {
      setLoadErr(e?.message ?? "rename failed");
    } finally {
      setEditingBusy(false);
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
              Ojas
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
            label so the section reads at a glance, Claude-style. The list
            itself is read from the shared SessionContext so updates from
            the chat page (LLM rename, auto-suffix) flow in here without
            any prop drilling or window events. */}
        {project && (() => {
          const sessions = sessionsStore.list(project.id);
          return (
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
                const isEditing = editingId === s.id;
                return (
                  <li key={s.id} className="group relative">
                    {isEditing ? (
                      <input
                        ref={editInputRef}
                        type="text"
                        value={editingValue}
                        onChange={(e) => setEditingValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void commitEdit(s);
                          } else if (e.key === "Escape") {
                            e.preventDefault();
                            cancelEdit();
                          }
                        }}
                        onBlur={() => {
                          if (!editingBusy && editingValue.trim() && editingValue.trim() !== s.name) {
                            void commitEdit(s);
                          } else {
                            cancelEdit();
                          }
                        }}
                        onClick={(e) => e.stopPropagation()}
                        disabled={editingBusy}
                        maxLength={128}
                        className="block w-full rounded-md border border-accent bg-bg px-3 py-1.5 pr-3 text-sm text-text outline-none focus:ring-2 focus:ring-accent/30"
                      />
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => openSession(s.id)}
                          className={`
                            block w-full truncate rounded-md px-3 py-1.5 pr-20 text-left text-sm transition-colors
                            ${isActive
                              ? "bg-accent/10 text-text"
                              : "text-muted hover:bg-elevated hover:text-text"}
                          `}
                          title={s.name}
                        >
                          {s.name}
                        </button>
                        {/* Edit (pencil) — always visible, neutral */}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            beginEdit(s);
                          }}
                          className="absolute right-7 top-1/2 inline-flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-subtle transition-colors hover:bg-elevated hover:text-text"
                          title="Rename chat"
                          aria-label={`Rename chat ${s.name}`}
                        >
                          <PencilIcon />
                        </button>
                        {/* Delete (trash) — always visible, red */}
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteSession(s);
                          }}
                          className="absolute right-1.5 top-1/2 inline-flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-subtle transition-colors hover:bg-danger/10 hover:text-danger"
                          title="Delete chat"
                          aria-label="Delete chat"
                        >
                          <TrashIcon />
                        </button>
                      </>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
          );
        })()}

        {/* Footer — workspace path, optional Admin link for root, Log out. */}
        <div className="border-t border-border px-3 py-3">
          {project && (
            <div
              className="mb-2.5 truncate font-mono text-[10px] text-subtle"
              title={project.workspace_path}
            >
              {project.workspace_path}
            </div>
          )}
          {/* Install Ojas as PWA — renders nothing once standalone. */}
          <div className="mb-2 empty:hidden"><InstallButton variant="primary" /></div>
          <Link
            to="/settings"
            className="mb-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-border bg-bg px-3 py-1.5 text-sm font-medium text-fg transition-colors hover:border-accent/40 hover:text-accent"
          >
            <CogIcon />
            <span>Settings</span>
          </Link>
          {me?.role === "root" && (
            <Link
              to="/admin"
              className="mb-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-accent/30 bg-accent/10 px-3 py-1.5 text-sm font-medium text-accent transition-colors hover:border-accent/50 hover:bg-accent/15"
            >
              <ShieldIcon />
              <span>Admin</span>
            </Link>
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
            <div className="flex flex-1 flex-col items-center justify-center px-6 py-10 text-center">
              <div
                aria-hidden
                className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-accent-gradient shadow-lift"
              >
                <span className="text-3xl font-bold text-white">O</span>
              </div>
              <h1 className="font-serif text-3xl font-semibold tracking-tight md:text-4xl">
                Welcome to Ojas
              </h1>
              <p className="mt-3 max-w-xl text-base text-muted">
                Your personal coding agent. Describe what you want in plain
                English — Ojas plans, writes, runs, and ships the code for
                you, autonomously.
              </p>

              <button
                type="button"
                onClick={startNew}
                className="btn-primary mt-6 min-h-touch px-6"
              >
                + Start a new chat
              </button>
              <p className="mt-3 text-xs text-muted">
                Or pick an existing chat from the sidebar to resume it.
              </p>

              {/* Try-saying examples. Concrete prompts make it obvious
                  what kind of asks Ojas handles best. Clicking one starts
                  a new chat with the prompt pre-filled (the agent picks
                  up the message when the user hits send). */}
              <div className="mt-10 w-full max-w-2xl">
                <p className="mb-3 text-xs font-medium uppercase tracking-wider text-muted">
                  Try saying
                </p>
                <div className="grid gap-2 text-left text-sm sm:grid-cols-2">
                  {[
                    "Build a small todo app I can install on my phone.",
                    "Make a snake game in vanilla JS.",
                    "Set up a FastAPI backend with a hello endpoint.",
                    "Add dark mode to my React app.",
                  ].map((prompt) => (
                    <button
                      key={prompt}
                      type="button"
                      onClick={startNew}
                      className="rounded-lg border border-border bg-elevated px-3 py-2.5 text-muted transition-colors hover:border-accent/40 hover:bg-surface hover:text-text"
                    >
                      &ldquo;{prompt}&rdquo;
                    </button>
                  ))}
                </div>
              </div>

              <p className="mt-10 text-xs text-muted/70">
                Tip: install Ojas to your home screen — it works like a
                native app. Look for the install button on this page.
              </p>
            </div>
          )
        ) : (
          <div className="flex flex-1 items-center justify-center px-6 py-8 text-center">
            <div>
              <div className="font-serif text-2xl font-semibold tracking-tight">
                {loadErr ? "Couldn't load workspace" : "Setting up your workspace…"}
              </div>
              <p className="mt-2 text-sm text-muted">
                {loadErr ?? "Getting your default project ready."}
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Name-conflict modal — real React <dialog>, not a native alert. */}
      {conflict && (
        <NameConflictModal
          desired={conflict.desired}
          existingName={conflict.existingName}
          existingId={conflict.existingId}
          onClose={() => setConflict(null)}
        />
      )}
      {deletingSession && (
        <DeleteProgressModal
          open={!!deletingSession}
          targetKind="session"
          targetId={deletingSession.id}
          targetName={deletingSession.name}
          job={deletingSession.job}
          onPoll={() =>
            sessionApi.deleteJobStatus(
              deletingSession.id,
              deletingSession.job!.job_id,
            )
          }
          onCancelJob={
            deletingSession.job
              ? () =>
                  sessionApi.cancelDelete(
                    deletingSession.id,
                    deletingSession.job!.job_id,
                  )
              : undefined
          }
          onClose={() => setDeletingSession(null)}
        />
      )}
    </div>
  );
}

// Name-conflict modal (same behavior as in SessionList.tsx). Real <dialog>
// so we get a focus trap + Esc to close + backdrop click to close. The
// "Open existing session →" button jumps the user to the conflicting
// session in the same project.
function NameConflictModal({
  desired,
  existingName,
  existingId,
  onClose,
}: {
  desired: string;
  existingName: string;
  existingId: string;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => {
        if (e.target === ref.current) ref.current?.close();
      }}
      className="rounded-xl border border-border bg-bg p-0 text-text shadow-2xl backdrop:bg-black/40"
    >
      <div className="w-[min(90vw,28rem)] p-5">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-warning/40 bg-warning/10 text-warning">
            ⚠
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="font-serif text-lg font-semibold leading-tight">
              Name already in use
            </h2>
            <p className="mt-1.5 text-sm text-muted">
              Another chat in this project is already named{" "}
              <span className="font-mono text-text">{existingName}</span>.
              Chat names must be unique within a project.
            </p>
            <p className="mt-2 text-xs text-muted">
              You tried to rename to{" "}
              <span className="font-mono text-text">{desired}</span>.
            </p>
          </div>
        </div>
        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onClose}
            className="btn-ghost min-h-touch"
            autoFocus
          >
            Try a different name
          </button>
          {existingId && (
            <a
              href={`/s/${existingId}`}
              className="btn-primary inline-flex min-h-touch items-center justify-center"
            >
              Open existing chat →
            </a>
          )}
        </div>
      </div>
    </dialog>
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
function ShieldIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}
function CogIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}
function PencilIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 113 3L7 19l-4 1 1-4 12.5-12.5z" />
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
