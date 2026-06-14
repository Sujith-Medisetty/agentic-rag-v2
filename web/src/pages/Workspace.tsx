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
import {
  MenuIcon, ChevronLeftIcon, TrashIcon, PlusIcon, LogoutIcon,
  ShieldIcon, CogIcon, PencilIcon,
} from "@/components/icons";

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
        const apiItems = await sessionsApi.list(p.id);
        if (cancelled) return;
        // /sessions returns the full list (newest-first), no envelope.
        // Merge: if startNew() ran while we were loading, the new session
        // is already in the context (from sessions.add()) but not in
        // `apiItems` (a stale snapshot). Keep locally-added sessions so they
        // don't vanish from the sidebar.
        sessionsStore.setAll(p.id, (() => {
          const apiIds = new Set(apiItems.map((s) => s.id));
          const localOnly = sessionsStore.list(p.id).filter(
            (s) => !apiIds.has(s.id),
          );
          return localOnly.length > 0 ? [...localOnly, ...apiItems] : apiItems;
        })());
        // Auto-open the most recent session when the user lands with no
        // session selected (e.g. logging in on a new device). Use the ref
        // so we read the *current* URL param, not the stale closure value.
        if (apiItems.length > 0 && !activeSessionIdRef.current) {
          navigate(`/s/${apiItems[0].id}`, { replace: true });
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

  // Same as startNew() but pre-fills the chat composer's input with
  // `prompt` once the new chat mounts. The handoff is via React
  // Router's `state` (not URL search params) so:
  //   1. The URL stays clean (/s/<id>, not /s/<id>?q=...)
  //   2. The text can contain spaces, quotes, em-dashes — anything —
  //      without URL-encoding noise
  //   3. Refreshing the chat page clears the prefill (the text is in
  //      navigation state, not in the URL), which is what the user
  //      would expect (a refresh = "start fresh on this chat")
  // We deliberately do NOT auto-send: the user wants to read the
  // pre-filled prompt, maybe edit it, then hit send themselves. Auto
  // -send would surprise them and burn a turn.
  const startNewWithPrefill = async (prompt: string) => {
    if (!project) return;
    try {
      const s = await sessionsApi.create(
        project.id,
        // Use the first ~40 chars of the prompt as the session name
        // (plus a timestamp suffix) so the sidebar entry is
        // recognisable — otherwise every "Try saying" click creates
        // a session literally named "Chat 6/14/2026, 10:42 AM" and
        // the user has no idea which is which.
        prompt.length > 40 ? `${prompt.slice(0, 40).trim()}…` : prompt,
      );
      sessionsStore.add(project.id, s);
      navigate(`/s/${s.id}`, { state: { prefill: prompt } });
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
            <ChevronLeftIcon className="h-4 w-4" />
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
            <PlusIcon className="h-3.5 w-3.5" />
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
                          <PencilIcon className="h-3.5 w-3.5" />
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
                          <TrashIcon className="h-3.5 w-3.5" />
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
            <CogIcon className="h-3.5 w-3.5" />
            <span>Settings</span>
          </Link>
          {me?.role === "root" && (
            <Link
              to="/admin"
              className="mb-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-accent/30 bg-accent/10 px-3 py-1.5 text-sm font-medium text-accent transition-colors hover:border-accent/50 hover:bg-accent/15"
            >
              <ShieldIcon className="h-3.5 w-3.5" />
              <span>Admin</span>
            </Link>
          )}
          <button
            onClick={logout}
            className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-danger/30 bg-danger/10 px-3 py-1.5 text-sm font-medium text-danger transition-colors hover:border-danger/50 hover:bg-danger/15"
          >
            <LogoutIcon className="h-3.5 w-3.5" />
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
            <MenuIcon className="h-4 w-4" />
          </button>
        )}

        {/* Either: active chat (via Outlet → ChatPage), welcome screen with
            CTA, or a loading state while the default project resolves. */}
        {project ? (
          activeSessionId ? (
            <Outlet context={{ project, sidebarOpen, isAdmin: me?.role === "root" }} />
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

              {/* Try-saying examples. Concrete, app-shaped prompts —
                  not "make a React app that does X" (mentioning a tech
                  stack turns the starter into a homework assignment).
                  These read like things a person would actually want
                  built. Clicking one starts a new chat with the prompt
                  pre-filled in the composer (the user still hits send
                  themselves, so they can edit it first). See
                  `startNewWithPrefill` for the navigation handoff. */}
              <div className="mt-10 w-full max-w-2xl">
                <p className="mb-3 text-xs font-medium uppercase tracking-wider text-muted">
                  Try saying
                </p>
                <div className="grid gap-2 text-left text-sm sm:grid-cols-2">
                  {[
                    "Build me a simple to-do list app.",
                    "Build me a calculator app.",
                    "Build me a snake and ladder board game.",
                    "Build me a small Instagram-style photo feed.",
                  ].map((prompt) => (
                    <button
                      key={prompt}
                      type="button"
                      onClick={() => startNewWithPrefill(prompt)}
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

// ── Icons ──────────────────────────────────────────────────────────────────
// (icons imported from @/components/icons)
