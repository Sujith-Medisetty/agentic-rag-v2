import { useEffect, useRef, useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { projectsApi, sessionsApi, ApiError } from "@/lib/api";
import type { Project, Session } from "@/lib/types";
import ProjectSettings from "@/components/ProjectSettings";

export default function SessionList() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const [project, setProject] = useState<Project | null>(null);
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
  const editInputRef = useRef<HTMLInputElement>(null);

  // React modal state for name conflicts. `null` = hidden. Otherwise:
  // the desired name that conflicted + the id+name of the existing
  // session that's already using it (so the user can jump to it).
  const [conflict, setConflict] = useState<{
    desired: string;
    existingId: string;
    existingName: string;
  } | null>(null);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr(null);
      try {
        const [p, ss] = await Promise.all([
          projectsApi.get(projectId),
          sessionsApi.list(projectId),
        ]);
        if (cancelled) return;
        setProject(p);
        setSessions(ss);
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
      const updated = await sessionsApi.rename(s.id, trimmed);
      setSessions((cur) => cur.map((x) => (x.id === s.id ? updated : x)));
      cancelEdit();
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 409) {
        // Server returned a structured 409 with the conflicting session's
        // id + name. Show the React modal so the user can jump to the
        // existing session instead.
        let detail: any = null;
        try {
          detail = JSON.parse(e.message);
        } catch {
          detail = null;
        }
        setConflict({
          desired: trimmed,
          existingId: detail?.existing_session_id ?? "",
          existingName:
            detail?.existing_session_name ?? "the existing session",
        });
        // Keep the edit input open so the user can try a different name.
      } else {
        setErr(e?.message ?? "rename failed");
      }
    } finally {
      setEditingBusy(false);
    }
  };

  const deleteSession = async (s: Session) => {
    if (!confirm(
      `Delete session "${s.name}"?\n\nThis permanently removes the chat history, plan state, and event log for this session. This cannot be undone.`
    )) return;
    try {
      await sessionsApi.remove(s.id);
      setSessions((prev) => prev.filter((x) => x.id !== s.id));
    } catch (e: any) {
      setErr(e?.message ?? "delete failed");
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

      {/* Name-conflict modal — a real React dialog, not a native alert(). */}
      {conflict && (
        <NameConflictModal
          desired={conflict.desired}
          existingName={conflict.existingName}
          existingId={conflict.existingId}
          projectId={projectId ?? ""}
          onClose={() => setConflict(null)}
        />
      )}
    </div>
  );
}

// =============================================================================
// Name-conflict modal
// =============================================================================
// Real <dialog> element (semantic + keyboard-friendly). Shows the user that
// the name they tried to use is already taken by another session in the
// same project, with two clear actions: jump to the existing session, or
// close and try a different name. Dismisses on Esc / backdrop click / X.
function NameConflictModal({
  desired,
  existingName,
  existingId,
  projectId,
  onClose,
}: {
  desired: string;
  existingName: string;
  existingId: string;
  projectId: string;
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
        // Click on the backdrop (outside the inner box) closes the dialog.
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
              Another session in this project is already named{" "}
              <span className="font-mono text-text">{existingName}</span>.
              Session names must be unique within a project.
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
          {existingId && projectId && (
            <a
              href={`/p/${projectId}/s/${existingId}`}
              className="btn-primary inline-flex min-h-touch items-center justify-center"
            >
              Open existing session →
            </a>
          )}
        </div>
      </div>
    </dialog>
  );
}

// =============================================================================
// Icons (match the ones in Admin.tsx — outline strokes for clarity at small
// sizes). Duplicated locally to keep the page self-contained.
// =============================================================================
function PencilIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 113 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  );
}

function TrashIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M3 6h18" />
      <path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2" />
      <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  );
}
