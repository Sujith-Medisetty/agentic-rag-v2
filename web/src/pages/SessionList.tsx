import { useEffect, useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { projectsApi, sessionsApi } from "@/lib/api";
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
        {sessions.map((s) => (
          <div key={s.id} className="list-card group flex items-center gap-3">
            <Link
              to={`/p/${projectId}/s/${s.id}`}
              className="min-w-0 flex-1"
            >
              <div className="truncate font-medium text-text">{s.name}</div>
              <div className="mt-0.5 text-xs text-muted">
                Last active {new Date(s.last_active_at * 1000).toLocaleString()}
              </div>
            </Link>
            <button
              type="button"
              onClick={async (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!confirm(
                  `Delete session "${s.name}"?\n\nThis permanently removes the chat history, plan state, and event log for this session. This cannot be undone.`
                )) return;
                try {
                  await sessionsApi.remove(s.id);
                  setSessions((prev) => prev.filter((x) => x.id !== s.id));
                } catch (e: any) {
                  setErr(e?.message ?? "delete failed");
                }
              }}
              className="min-h-touch min-w-touch rounded-md border border-border/60 px-2 text-sm text-muted hover:border-danger/40 hover:bg-danger/10 hover:text-danger"
              title="Delete session (chat + plan + events; project workspace untouched)"
              aria-label={`Delete session ${s.name}`}
            >
              Delete
            </button>
            <Link
              to={`/p/${projectId}/s/${s.id}`}
              className="text-subtle hover:text-accent"
            >
              →
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}
