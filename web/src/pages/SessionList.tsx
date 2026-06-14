import { useEffect, useRef, useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import {
  projectsApi, sessionsApi, sessionApi,
  type DeleteJobStart,
} from "@/lib/api";
import type { Project, Session } from "@/lib/types";
import ProjectSettings from "@/components/ProjectSettings";
import { PencilIcon, TrashIcon } from "@/components/icons";
import DeleteProgressModal from "@/components/DeleteProgressModal";

export default function SessionList() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const [project, setProject] = useState<Project | null>(null);
  // Local list state — this page does NOT write to the shared
  // SessionContext. The sidebar in Workspace loads its own list on
  // mount, and the SessionList page lives at /p/:id/sessions (no
  // sidebar), so mutating the shared context from here would just
  // risk clobbering the sidebar's own (possibly larger) list with
  // a 50-item subset.
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  // Inline rename state. `editingId` is the session currently being
  // renamed; the input value lives in `editingValue`; `editingBusy`
  // disables the input while the PATCH is in flight.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [editingBusy, setEditingBusy] = useState(false);
  // DeleteProgressModal state. Same pattern as Workspace.tsx.
  const [deletingSession, setDeletingSession] = useState<{
    id: string;
    name: string;
    job: DeleteJobStart | null;
  } | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);

  // Fetch the project + ALL sessions for the project in one round trip.
  // The server returns the full list (newest-first) — no pagination.
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true);
    setErr(null);
    (async () => {
      try {
        const [p, list] = await Promise.all([
          projectsApi.get(projectId),
          sessionsApi.list(projectId),
        ]);
        if (cancelled) return;
        setProject(p);
        setSessions(list);
      } catch (e: any) {
        if (!cancelled) setErr(e?.message ?? "failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Focus the input when edit mode opens.
  useEffect(() => {
    if (editingId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingId]);

  const startNew = async () => {
    if (!projectId) return;
    setCreating(true);
    try {
      const s = await sessionsApi.create(
        projectId,
        `Session ${new Date().toLocaleString()}`,
      );
      navigate(`/p/${projectId}/s/${s.id}`);
    } catch (e: any) {
      setErr(e?.message ?? "failed to create session");
    } finally {
      setCreating(false);
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
    if (!trimmed) {
      cancelEdit();
      return;
    }
    if (trimmed === s.name) {
      cancelEdit();
      return;
    }
    setEditingBusy(true);
    try {
      const { session, wasSuffixed, actualName } =
        await sessionsApi.renameWithSufStatus(s.id, trimmed);
      // Update the row in the local list so the new name shows
      // immediately. If the new name collides with another session
      // we don't show the conflict modal here (the server suffix
      // already handled it); the toast below tells the user.
      setSessions((prev) =>
        prev.map((x) => (x.id === s.id ? session : x)),
      );
      if (wasSuffixed && actualName !== trimmed) {
        setErr(
          `Renamed to "${actualName}" — "${trimmed}" was already taken.`,
        );
        // Clear the message after a few seconds so it doesn't linger.
        setTimeout(() => setErr(null), 4000);
      }
      cancelEdit();
    } catch (e: any) {
      setErr(e?.message ?? "rename failed");
    } finally {
      setEditingBusy(false);
    }
  };

  const deleteSession = async (s: Session) => {
    if (!confirm(
      `Delete session "${s.name}"?\n\n` +
      `This permanently removes the chat history, the agent's edited files, ` +
      `the sub-project build output, and any live URLs + systemd units for ` +
      `this session. The entry disappears immediately; the server cleanup ` +
      `runs in the background.\n\nThis cannot be undone.`
    )) return;
    // 1. Cancel any in-flight agent turn (best-effort).
    sessionApi.cancel(s.id).catch(() => {});
    // 2. Optimistic local removal.
    setSessions((prev) => prev.filter((x) => x.id !== s.id));
    // 3. Kick off the async server-side teardown + open the progress modal.
    try {
      const job = await sessionApi.startDelete(s.id);
      setDeletingSession({ id: s.id, name: s.name, job });
    } catch (e: any) {
      setErr(e?.message ?? "delete failed to start");
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6 px-4 pb-12 pt-6 sm:px-6 sm:pt-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <div className="min-w-0 flex-1">
          <Link
            to="/"
            className="inline-flex items-center gap-1 text-xs text-muted transition-colors hover:text-accent"
          >
            ← All projects
          </Link>
          <h1 className="mt-2 truncate font-serif text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
            {project?.name ?? "Loading…"}
          </h1>
          {project && (
            <div className="mt-1.5 truncate font-mono text-xs text-muted">
              {project.workspace_path}
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2 sm:self-start sm:pt-1">
          <button
            onClick={() => setShowSettings((v) => !v)}
            className="btn-ghost min-h-touch"
            aria-label="Project settings"
          >
            Settings
          </button>
          <button
            onClick={startNew}
            disabled={creating || !projectId}
            className="btn-primary min-h-touch"
          >
            {creating ? "Creating…" : "+ New session"}
          </button>
        </div>
      </div>

      {showSettings && project && (
        <ProjectSettings
          project={project}
          onChange={(updated) => setProject(updated)}
        />
      )}

      {loading && <div className="text-muted">Loading…</div>}
      {err && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {err}
        </div>
      )}

      {!loading && !err && sessions.length === 0 && (
        <div className="glass-card-soft p-10 text-center">
          <div className="text-base text-text">No sessions yet</div>
          <div className="mt-1 text-sm text-muted">
            Start a new one to begin chatting.
          </div>
        </div>
      )}

      <div className="grid gap-2.5">
        {sessions.map((s) => {
          const isEditing = editingId === s.id;
          return (
            <div
              key={s.id}
              className="list-card flex items-center gap-2 sm:gap-3"
            >
              {isEditing ? (
                // Inline rename input. Saves on Enter, cancels on Esc.
                <div className="min-w-0 flex-1">
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
                      // Save on blur unless we're already saving / canceling.
                      if (!editingBusy && editingValue.trim() && editingValue.trim() !== s.name) {
                        void commitEdit(s);
                      } else {
                        cancelEdit();
                      }
                    }}
                    disabled={editingBusy}
                    maxLength={128}
                    className="w-full rounded border border-accent bg-bg px-2 py-1 font-medium text-text outline-none focus:ring-2 focus:ring-accent/30"
                    placeholder="Session name"
                  />
                  <div className="mt-0.5 text-xs text-muted">
                    Press Enter to save · Esc to cancel
                  </div>
                </div>
              ) : (
                <Link
                  to={`/p/${projectId}/s/${s.id}`}
                  className="min-w-0 flex-1"
                >
                  <div className="truncate font-medium text-text">{s.name}</div>
                  <div className="mt-0.5 text-xs text-muted">
                    Last active {new Date(s.last_active_at * 1000).toLocaleString()}
                  </div>
                </Link>
              )}
              {/* Edit (pencil) — always visible */}
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  isEditing ? cancelEdit() : beginEdit(s);
                }}
                className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-elevated text-text transition-colors hover:border-accent hover:text-accent"
                title={isEditing ? "Cancel rename" : "Rename session"}
                aria-label={isEditing ? "Cancel rename" : `Rename session ${s.name}`}
              >
                <PencilIcon className="h-4 w-4" />
              </button>
              {/* Delete (trash) — always visible */}
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  void deleteSession(s);
                }}
                className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-danger/30 bg-danger/10 text-danger transition-colors hover:border-danger/50 hover:bg-danger/15"
                title="Delete session (chat + plan + events; project workspace untouched)"
                aria-label={`Delete session ${s.name}`}
              >
                <TrashIcon className="h-4 w-4" />
              </button>
              {!isEditing && (
                <Link
                  to={`/p/${projectId}/s/${s.id}`}
                  className="hidden shrink-0 text-subtle hover:text-accent sm:inline"
                  aria-label={`Open session ${s.name}`}
                >
                  →
                </Link>
              )}
            </div>
          );
        })}
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

// (icons imported from @/components/icons)
