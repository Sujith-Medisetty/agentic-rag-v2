import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { projectsApi, ApiError } from "@/lib/api";
import type { Project } from "@/lib/types";

export default function ProjectList() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  const load = async () => {
    setLoading(true);
    setErr(null);
    try {
      setProjects(await projectsApi.list());
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "failed to load");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="mx-auto max-w-3xl space-y-8 p-6 pt-8">
      <div className="flex items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-subtle">
            Workspace
          </div>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight">
            Projects
          </h1>
          <p className="mt-1 text-sm text-muted">
            Each project points at a local repo. Open one to start a session.
          </p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary">
          + New project
        </button>
      </div>

      {loading && <div className="text-muted">Loading…</div>}
      {err && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {err}
        </div>
      )}

      {!loading && !err && projects.length === 0 && (
        <div className="glass-card-soft p-10 text-center">
          <div className="text-base text-text">No projects yet</div>
          <div className="mt-1 text-sm text-muted">
            Create one to get started.
          </div>
        </div>
      )}

      <div className="grid gap-2.5">
        {projects.map((p) => (
          <Link key={p.id} to={`/p/${p.id}`} className="list-card">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate font-medium text-text">{p.name}</div>
                <div className="mt-0.5 truncate font-mono text-xs text-muted">
                  {p.workspace_path}
                </div>
              </div>
              <span className="text-subtle transition-colors group-hover:text-accent">
                →
              </span>
            </div>
          </Link>
        ))}
      </div>

      {showCreate && (
        <CreateProjectModal
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            load();
          }}
        />
      )}
    </div>
  );
}

function CreateProjectModal({
  onClose, onCreated,
}: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await projectsApi.create(name.trim(), path.trim());
      onCreated();
    } catch (e: any) {
      setErr(e?.message ?? "failed to create");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="glass-card w-full max-w-md space-y-4 p-6 animate-fade-in-up"
      >
        <div>
          <h2 className="text-lg font-semibold tracking-tight">New project</h2>
          <p className="mt-0.5 text-xs text-muted">
            Point this at a local repo on disk.
          </p>
        </div>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Project name (e.g. agentic-rag)"
          className="field"
        />
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="Workspace path (e.g. ~/code/my-repo)"
          className="field font-mono text-sm"
        />
        {err && (
          <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="btn-ghost">
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim() || !path.trim()}
            className="btn-primary"
          >
            {busy ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}
