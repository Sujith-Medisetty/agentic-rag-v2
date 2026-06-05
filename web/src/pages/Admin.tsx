// Admin panel — root only. Lists every user + every running process on the
// VM, with a kill button per process. Auto-refreshes the process list every
// 3 seconds so newly-spawned dev servers / hung builds show up without a
// manual reload.

import { useEffect, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { authApi, adminApi, deployedAppsApi, ApiError } from "@/lib/api";
import type { AuthUser, AdminProcess, DeployedApp } from "@/lib/api";

export default function Admin() {
  const [me, setMe] = useState<AuthUser | "loading" | "denied">("loading");
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [procs, setProcs] = useState<AdminProcess[]>([]);
  const [apps, setApps] = useState<DeployedApp[]>([]);
  const [err, setErr] = useState<string | null>(null);

  // Resolve current user once. Anyone non-root gets redirected.
  useEffect(() => {
    authApi.me()
      .then((u) => setMe(u.role === "root" ? u : "denied"))
      .catch(() => setMe("denied"));
  }, []);

  // Once we know we're root, load users (one-shot) and processes (polled).
  useEffect(() => {
    if (me === "loading" || me === "denied") return;
    void load();
    const id = setInterval(loadProcs, 3000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me]);

  const load = async () => {
    try {
      const [us, ps, as] = await Promise.all([
        adminApi.users(), adminApi.processes(), deployedAppsApi.list(),
      ]);
      setUsers(us);
      setProcs(ps);
      setApps(as);
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "failed to load");
    }
  };
  const loadProcs = async () => {
    try { setProcs(await adminApi.processes()); } catch { /* keep last */ }
  };

  const kill = async (pid: number) => {
    if (!confirm(`Kill process ${pid}? It will be sent SIGTERM.`)) return;
    try {
      await adminApi.killProcess(pid);
      setProcs((p) => p.filter((x) => x.pid !== pid));
    } catch (e: any) {
      alert(`Kill failed: ${e?.message ?? "unknown error"}`);
    }
  };

  const takedownApp = async (slug: string) => {
    if (!confirm(`Take down "${slug}"? Files at /opt/ojas-apps/${slug}/ will be deleted and the public URL will return 404.`)) return;
    try {
      await deployedAppsApi.delete(slug);
      setApps((a) => a.filter((x) => x.slug !== slug));
    } catch (e: any) {
      alert(`Takedown failed: ${e?.message ?? "unknown error"}`);
    }
  };

  if (me === "loading") {
    return (
      <div className="flex h-screen items-center justify-center text-muted">
        Loading…
      </div>
    );
  }
  if (me === "denied") return <Navigate to="/" replace />;

  return (
    <div className="mx-auto max-w-5xl space-y-8 px-4 pb-12 pt-6 sm:px-6 sm:pt-8">
      <div className="flex items-end justify-between gap-4">
        <div>
          <Link
            to="/"
            className="text-xs text-muted transition-colors hover:text-accent"
          >
            ← Back to workspace
          </Link>
          <h1 className="mt-2 font-serif text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
            Admin
          </h1>
          <p className="mt-1.5 text-sm text-muted">
            Logged in as <span className="font-mono text-text">{me.email}</span> (root).
            Full control of this VM's Ojas state.
          </p>
        </div>
      </div>

      {err && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {err}
        </div>
      )}

      {/* ───── Processes ──────────────────────────────────────────────── */}
      <section className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
            Running processes
          </h2>
          <span className="text-tx-xs text-subtle">
            auto-refreshes every 3s · {procs.length} tracked
          </span>
        </div>
        {procs.length === 0 ? (
          <div className="glass-card-soft p-6 text-center text-sm text-muted">
            No tracked background processes right now.
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <thead className="bg-elevated/60 text-tx-xs uppercase tracking-wide text-subtle">
                <tr>
                  <th className="px-3 py-2">PID</th>
                  <th className="px-3 py-2">Port</th>
                  <th className="px-3 py-2">Command</th>
                  <th className="px-3 py-2">Session</th>
                  <th className="px-3 py-2">Started</th>
                  <th className="px-3 py-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {procs.map((p) => (
                  <tr key={p.pid} className="border-t border-border/60">
                    <td className="px-3 py-2 font-mono text-tx-xs">{p.pid}</td>
                    <td className="px-3 py-2 font-mono text-tx-xs">
                      {p.port ?? "—"}
                    </td>
                    <td
                      className="max-w-[24rem] truncate px-3 py-2 font-mono text-tx-xs"
                      title={p.command}
                    >
                      {p.command}
                    </td>
                    <td className="px-3 py-2 font-mono text-tx-xs text-muted">
                      <Link
                        to={`/s/${p.session_id}`}
                        className="hover:text-accent"
                        title="Open session"
                      >
                        {p.session_id.slice(0, 8)}…
                      </Link>
                    </td>
                    <td className="px-3 py-2 text-tx-xs text-muted">
                      {new Date(p.started_at * 1000).toLocaleTimeString()}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        onClick={() => kill(p.pid)}
                        className="rounded-md border border-danger/30 bg-danger/10 px-2 py-1 text-tx-xs font-medium text-danger hover:bg-danger/15"
                      >
                        Kill
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ───── Users ──────────────────────────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
          Users
        </h2>
        {users.length === 0 ? (
          <div className="glass-card-soft p-6 text-center text-sm text-muted">
            No accounts yet.
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <thead className="bg-elevated/60 text-tx-xs uppercase tracking-wide text-subtle">
                <tr>
                  <th className="px-3 py-2">Email</th>
                  <th className="px-3 py-2">Role</th>
                  <th className="px-3 py-2">Created</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id} className="border-t border-border/60">
                    <td className="px-3 py-2 font-mono text-tx-xs">{u.email}</td>
                    <td className="px-3 py-2">
                      <span
                        className={`pill text-tx-xs ${
                          u.role === "root" ? "pill-accent" : ""
                        }`}
                      >
                        {u.role}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-tx-xs text-muted">
                      {new Date(u.created_at * 1000).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ===== Deployed apps ============================================ */}
      <section>
        <div className="flex items-baseline justify-between">
          <h2 className="font-serif text-xl font-semibold tracking-tight">
            Deployed apps
          </h2>
          <span className="text-tx-xs text-muted">
            {apps.length} {apps.length === 1 ? "app" : "apps"} live
          </span>
        </div>
        <p className="mt-1 text-sm text-muted">
          Promoted session builds. Each app is static files served at
          <span className="ml-1 font-mono">/apps/&lt;slug&gt;/</span>
          — survives session delete + backend restart. Take down removes
          the on-disk files + DB row.
        </p>
        {apps.length === 0 ? (
          <div className="mt-3 rounded-md border border-border bg-elevated px-4 py-6 text-center text-sm text-muted">
            No apps deployed yet. Click 🚀 Deploy next to a Preview banner in a chat.
          </div>
        ) : (
          <div className="mt-3 overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-elevated text-left text-tx-xs uppercase tracking-wider text-muted">
                <tr>
                  <th className="px-3 py-2">Slug / URL</th>
                  <th className="px-3 py-2">Name</th>
                  <th className="px-3 py-2">Owner</th>
                  <th className="px-3 py-2">Deployed</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {apps.map((a) => {
                  const fullUrl = `${window.location.origin}/apps/${a.slug}/`;
                  const ownerEmail = users.find((u) => u.id === a.owner_user_id)?.email
                    ?? (a.owner_user_id ? a.owner_user_id.slice(0, 8) : "(orphan)");
                  const last = new Date(a.last_redeploy_at * 1000).toLocaleString();
                  return (
                    <tr key={a.slug} className="border-t border-border/60">
                      <td className="px-3 py-2">
                        <a
                          href={fullUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="font-mono text-tx-xs text-accent hover:underline"
                          title={fullUrl}
                        >
                          /apps/{a.slug}/
                        </a>
                      </td>
                      <td className="px-3 py-2 text-tx-xs">{a.name}</td>
                      <td className="px-3 py-2 font-mono text-tx-xs text-muted">{ownerEmail}</td>
                      <td className="px-3 py-2 text-tx-xs text-muted">{last}</td>
                      <td className="px-3 py-2 text-right">
                        <button
                          type="button"
                          onClick={() => takedownApp(a.slug)}
                          className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1 text-tx-xs font-medium text-danger hover:border-danger/50 hover:bg-danger/15"
                        >
                          Take down
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
