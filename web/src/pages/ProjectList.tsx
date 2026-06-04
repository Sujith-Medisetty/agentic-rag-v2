import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { projectsApi, pathsApi, ApiError } from "@/lib/api";
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
          <div key={p.id} className="list-card flex items-center gap-3">
            <Link to={`/p/${p.id}`} className="min-w-0 flex-1">
              <div className="truncate font-medium text-text">{p.name}</div>
              <div className="mt-0.5 truncate font-mono text-xs text-muted">
                {p.workspace_path}
              </div>
            </Link>
            <button
              type="button"
              onClick={async () => {
                if (!confirm(
                  `Delete project "${p.name}"?\n\nThis permanently removes ALL its sessions, messages, and events. Files on disk at ${p.workspace_path} are NOT touched.\n\nThis cannot be undone.`
                )) return;
                try {
                  await projectsApi.remove(p.id);
                  load();
                } catch (e) {
                  alert(`Delete failed: ${e instanceof ApiError ? e.message : "unknown"}`);
                }
              }}
              className="min-h-touch min-w-touch rounded-md border border-border/60 px-2 text-sm text-muted hover:border-danger/40 hover:bg-danger/10 hover:text-danger"
              title="Delete project (sessions + messages + events; files on disk untouched)"
              aria-label={`Delete project ${p.name}`}
            >
              Delete
            </button>
            <Link to={`/p/${p.id}`} className="text-subtle transition-colors hover:text-accent">
              →
            </Link>
          </div>
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
  const [locations, setLocations] = useState<{ label: string; path: string }[]>([]);
  const [showBrowser, setShowBrowser] = useState(false);

  useEffect(() => {
    // Fetch the user's common dev-directory locations so we can offer
    // prefill chips. Browsers don't expose a native filesystem picker that
    // works across iOS / Safari / Firefox, so this is the practical
    // middle-ground: skip the typing, just tap a chip + append a folder.
    pathsApi.common().then((r) => setLocations(r.locations)).catch(() => {});
  }, []);

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
            Point this at a local folder. The agent will only read/write
            inside this folder — never outside it.
          </p>
        </div>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Project name (e.g. my-app)"
          className="field"
        />
        <div className="space-y-1.5">
          <div className="flex items-stretch gap-1.5">
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="Workspace path (e.g. ~/code/my-repo)"
              className="field flex-1 font-mono text-sm"
            />
            <button
              type="button"
              onClick={() => setShowBrowser(true)}
              className="min-h-touch shrink-0 rounded-md border border-border/60 bg-elevated/40 px-3 text-sm text-muted hover:border-accent/40 hover:bg-accent/10 hover:text-accent"
              title="Browse for a folder"
              aria-label="Browse for a folder"
            >
              Browse…
            </button>
          </div>
          {locations.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              <span className="self-center text-[11px] uppercase tracking-[0.16em] text-subtle">
                Quick pick:
              </span>
              {locations.map((loc) => (
                <button
                  key={loc.path}
                  type="button"
                  onClick={() => {
                    const base = loc.path.replace(/\/+$/, "");
                    setPath(base + "/");
                  }}
                  className="rounded-full border border-border/60 px-2.5 py-0.5 text-xs text-muted hover:border-accent/40 hover:bg-accent/10 hover:text-accent"
                  title={loc.path}
                >
                  {loc.label}
                </button>
              ))}
            </div>
          )}
        </div>
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
      {showBrowser && (
        <FolderBrowser
          startPath={path.trim() || undefined}
          onPick={(p) => { setPath(p); setShowBrowser(false); }}
          onClose={() => setShowBrowser(false)}
        />
      )}
    </div>
  );
}

function FolderBrowser({
  startPath, onPick, onClose,
}: {
  startPath?: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [cwd, setCwd] = useState<string>("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<{ name: string; path: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const navigate = async (target?: string) => {
    setLoading(true);
    setErr(null);
    try {
      const res = await pathsApi.browse(target);
      setCwd(res.cwd);
      setParent(res.parent);
      setEntries(res.entries);
    } catch (e: any) {
      setErr(e?.message ?? "failed to read directory");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // If the user already typed a partial path, try to start there; if it
    // 404s (typo, doesn't exist yet), fall back to home.
    void navigate(startPath);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      className="fixed inset-0 z-40 flex items-end justify-center bg-black/65 backdrop-blur-sm sm:items-center"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex h-[80vh] w-full max-w-md flex-col rounded-t-2xl border border-border bg-surface shadow-lift sm:h-[70vh] sm:rounded-2xl"
      >
        <div className="border-b border-border/60 px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold tracking-tight">
              Choose a folder
            </h3>
            <button
              type="button"
              onClick={onClose}
              className="text-muted hover:text-text"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
          <div
            className="mt-1 truncate font-mono text-xs text-muted"
            title={cwd}
          >
            {cwd || "Loading…"}
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {parent !== null && (
            <button
              type="button"
              onClick={() => navigate(parent)}
              disabled={loading}
              className="flex w-full items-center gap-2 border-b border-border/40 px-4 py-3 text-left text-sm text-muted hover:bg-elevated/60"
            >
              <span className="font-mono">..</span>
              <span>parent folder</span>
            </button>
          )}
          {loading && (
            <div className="px-4 py-6 text-center text-sm text-muted">
              Loading…
            </div>
          )}
          {err && (
            <div className="mx-4 mt-3 rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
              {err}
            </div>
          )}
          {!loading && !err && entries.length === 0 && (
            <div className="px-4 py-6 text-center text-sm text-muted">
              (no subfolders here)
            </div>
          )}
          {!loading && entries.map((e) => (
            <button
              key={e.path}
              type="button"
              onClick={() => navigate(e.path)}
              className="flex w-full items-center gap-2 border-b border-border/30 px-4 py-3 text-left text-sm hover:bg-elevated/60"
            >
              <span className="text-muted">▸</span>
              <span className="truncate text-text">{e.name}</span>
            </button>
          ))}
        </div>
        <div className="border-t border-border/60 bg-elevated/40 p-3">
          <button
            type="button"
            onClick={() => cwd && onPick(cwd)}
            disabled={!cwd || loading}
            className="btn-primary w-full"
          >
            Use this folder
          </button>
          <div className="mt-1.5 text-center font-mono text-[11px] text-subtle">
            {cwd || "—"}
          </div>
        </div>
      </div>
    </div>
  );
}
