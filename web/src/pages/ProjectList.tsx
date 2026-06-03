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
    <div className="mx-auto max-w-3xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Projects</h1>
        <button
          onClick={() => setShowCreate(true)}
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-bg"
        >
          + New project
        </button>
      </div>

      {loading && <div className="text-muted">Loading…</div>}
      {err && <div className="text-danger">{err}</div>}

      {!loading && !err && projects.length === 0 && (
        <div className="rounded border border-dashed border-border p-8 text-center text-muted">
          No projects yet. Create one to get started.
        </div>
      )}

      <div className="grid gap-2">
        {projects.map((p) => (
          <Link
            key={p.id}
            to={`/p/${p.id}`}
            className="block rounded border border-border bg-surface p-4 transition hover:border-accent"
          >
            <div className="font-medium">{p.name}</div>
            <div className="font-mono text-xs text-muted">{p.workspace_path}</div>
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
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/60 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-md space-y-4 rounded-lg border border-border bg-surface p-6"
      >
        <h2 className="text-lg font-semibold">New project</h2>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Project name (e.g. agentic-rag)"
          className="w-full rounded border border-border bg-elevated px-3 py-2 outline-none focus:border-accent"
        />
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="Workspace path (e.g. ~/code/my-repo)"
          className="w-full rounded border border-border bg-elevated px-3 py-2 font-mono text-sm outline-none focus:border-accent"
        />
        {err && <div className="text-sm text-danger">{err}</div>}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-border px-3 py-1.5 text-sm"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim() || !path.trim()}
            className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-bg disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}
