// Admin panel — root only. Lists every user + every running process on the
// VM, with a kill button per process. Auto-refreshes the process list every
// 3 seconds so newly-spawned dev servers / hung builds show up without a
// manual reload.

import { useEffect, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { authApi, adminApi, ApiError } from "@/lib/api";
import type { AuthUser, AdminProcess } from "@/lib/api";

export default function Admin() {
  const [me, setMe] = useState<AuthUser | "loading" | "denied">("loading");
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [procs, setProcs] = useState<AdminProcess[]>([]);
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
      const [us, ps] = await Promise.all([adminApi.users(), adminApi.processes()]);
      setUsers(us);
      setProcs(ps);
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
    </div>
  );
}
