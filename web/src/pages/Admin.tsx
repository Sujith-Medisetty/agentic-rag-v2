// Admin panel — root only. Lists every user + every Ojas-owned service
// (main backend, caddy, deployed apps, MCP servers, anything we discover
// on a listening port) + every agent-spawned process on the VM. Each
// section has a per-row kill/takedown action where it makes sense.
// Auto-refreshes the process/service lists every 3 seconds so newly-
// spawned dev servers / hung builds / new deploys show up without a
// manual reload.

import { useEffect, useMemo, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { authApi, adminApi, deployedAppsApi, ApiError } from "@/lib/api";
import type {
  AuthUser, AdminProcess, OjasService, DeployedApp,
} from "@/lib/api";
import { KeyIcon, TrashIcon } from "@/components/icons";

type SourceFilter = "all" | "ojas-main" | "ojas-proxy" | "ojas-deployed" | "ojas-mcp" | "ojas-external";

export default function Admin() {
  const [me, setMe] = useState<AuthUser | "loading" | "denied">("loading");
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [procs, setProcs] = useState<AdminProcess[]>([]);
  const [services, setServices] = useState<OjasService[]>([]);
  const [apps, setApps] = useState<DeployedApp[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");

  // Resolve current user once. Anyone non-root gets redirected.
  useEffect(() => {
    authApi.me()
      .then((u) => setMe(u.role === "root" ? u : "denied"))
      .catch(() => setMe("denied"));
  }, []);

  // Once we know we're root, load users (one-shot) and processes/services
  // (polled). Users + deployed apps only change on explicit actions so
  // we reload them after every mutation rather than polling.
  useEffect(() => {
    if (me === "loading" || me === "denied") return;
    void load();
    // Live counts: procs + services change continuously → poll every 3s.
    const idLive = setInterval(loadLive, 3000);
    // Slower-changing data: new user signups + new app deploys → poll every 10s.
    const idSlow = setInterval(loadSlow, 10000);
    return () => {
      clearInterval(idLive);
      clearInterval(idSlow);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me]);

  const load = async () => {
    try {
      const [us, ps, svcs, as] = await Promise.all([
        adminApi.users(),
        adminApi.processes(),
        adminApi.services(),
        deployedAppsApi.list(),
      ]);
      setUsers(us);
      setProcs(ps);
      setServices(svcs);
      setApps(as);
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "failed to load");
    }
  };
  const loadLive = async () => {
    try {
      const [ps, svcs] = await Promise.all([
        adminApi.processes(),
        adminApi.services(),
      ]);
      setProcs(ps);
      setServices(svcs);
    } catch {
      /* keep last */
    }
  };
  const loadSlow = async () => {
    // Users + deployed apps: change rarely but the admin may have multiple
    // devices open, or someone may sign up / deploy from a different
    // session. A 10s poll is cheap and keeps the panel honest.
    try {
      const [us, as] = await Promise.all([
        adminApi.users(),
        deployedAppsApi.list(),
      ]);
      setUsers(us);
      setApps(as);
    } catch {
      /* keep last */
    }
  };

  const kill = async (pid: number, label: string) => {
    if (!confirm(`Kill process ${pid} (${label})? It will be sent SIGTERM.`)) return;
    try {
      await adminApi.killProcess(pid);
      setProcs((p) => p.filter((x) => x.pid !== pid));
    } catch (e: any) {
      alert(`Kill failed: ${e?.message ?? "unknown error"}`);
    }
  };

  const purgeDead = async () => {
    const dead = procs.filter((p) => !p.is_alive);
    if (dead.length === 0) return;
    if (!confirm(`Drop ${dead.length} stale process row(s) for already-dead PIDs? (The processes are already gone — this just cleans the DB so the list stays tidy.)`)) return;
    // No bulk endpoint; send N kills. They all no-op on the OS side and
    // just unregister the DB row (the backend kill endpoint is idempotent).
    for (const p of dead) {
      try {
        await adminApi.killProcess(p.pid);
      } catch {
        /* keep going */
      }
    }
    setProcs((cur) => cur.filter((x) => x.is_alive));
  };

  const takedownApp = async (slug: string) => {
    if (!confirm(`Take down "${slug}"? Files at /opt/ojas-apps/${slug}/ will be deleted and the public URL will return 404.`)) return;
    try {
      await deployedAppsApi.delete(slug);
      setApps((a) => a.filter((x) => x.slug !== slug));
      // Service row for this app is also gone — refresh.
      void loadLive();
    } catch (e: any) {
      alert(`Takedown failed: ${e?.message ?? "unknown error"}`);
    }
  };

  const deleteUser = async (u: AuthUser) => {
    if (me !== "loading" && me !== "denied" && u.id === me.id) {
      alert("You can't delete the account you're currently logged in as. Open a private window and log in as another root first.");
      return;
    }
    if (!confirm(
      `Delete user "${u.email}" (role=${u.role})?\n\n` +
      `This will:\n` +
      `  • remove their login (all their auth tokens are revoked)\n` +
      `  • CASCADE-delete any projects + sessions they own\n` +
      `  • orphan any deployed apps they own (files stay, owner becomes null)\n\n` +
      `This cannot be undone. Type the email to confirm:`,
    )) return;
    const confirmEmail = prompt(`Type the email to confirm deletion of "${u.email}":`);
    if (confirmEmail !== u.email) {
      alert("Email didn't match. Aborting.");
      return;
    }
    try {
      await adminApi.deleteUser(u.id);
      setUsers((cur) => cur.filter((x) => x.id !== u.id));
    } catch (e: any) {
      alert(`Delete failed: ${e?.message ?? "unknown error"}`);
    }
  };

  const resetUserPassword = async (u: AuthUser) => {
    const newPassword = prompt(
      `Set a new password for "${u.email}".\n\n` +
      `Must be at least 6 characters. The user will have to log in again with the new password.`,
      "",
    );
    if (newPassword === null) return; // cancel
    if (newPassword.length < 6) {
      alert("Password must be at least 6 characters.");
      return;
    }
    try {
      await adminApi.resetUserPassword(u.id, newPassword);
      alert(`Password for ${u.email} has been reset. They will be signed out of all devices.`);
    } catch (e: any) {
      alert(`Reset failed: ${e?.message ?? "unknown error"}`);
    }
  };

  // Derived: filtered services for the dropdown
  const filteredServices = useMemo(
    () => sourceFilter === "all"
      ? services
      : services.filter((s) => s.source === sourceFilter),
    [services, sourceFilter],
  );

  // Derived: count of each source for the filter chips
  const sourceCounts = useMemo(() => {
    const m: Record<string, number> = { all: services.length };
    for (const s of services) m[s.source] = (m[s.source] ?? 0) + 1;
    return m;
  }, [services]);

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

      {/* ───── Services (Ojas-owned processes + ports) ──────────────── */}
      <section className="space-y-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
            Services &amp; ports
          </h2>
          <span className="text-tx-xs text-subtle">
            auto-refreshes every 3s · {services.length} tracked
          </span>
        </div>
        <p className="text-tx-xs text-muted">
          Every process Ojas started (or is aware of) and every port it owns.
          Includes the main backend, the caddy reverse proxy, every deployed
          app's public URL, and any listening port the kernel hands us that
          we didn't expect.
        </p>
        {/* Source filter chips */}
        <div className="flex flex-wrap gap-1.5">
          {(["all", "ojas-main", "ojas-proxy", "ojas-deployed", "ojas-mcp", "ojas-external"] as SourceFilter[]).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setSourceFilter(f)}
              className={`pill text-tx-xs ${sourceFilter === f ? "pill-accent" : ""}`}
            >
              {f} <span className="opacity-60">({sourceCounts[f] ?? 0})</span>
            </button>
          ))}
        </div>
        {filteredServices.length === 0 ? (
          <div className="glass-card-soft p-6 text-center text-sm text-muted">
            No Ojas services registered yet. Restart the backend to register
            the main process + caddy.
          </div>
        ) : (
          <div className="max-h-[60vh] overflow-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 z-10 bg-elevated text-tx-xs uppercase tracking-wide text-subtle shadow-[0_1px_0_0_rgba(0,0,0,0.08)]">
                <tr>
                  <th className="px-3 py-2">Source</th>
                  <th className="px-3 py-2">Label</th>
                  <th className="px-3 py-2">PID</th>
                  <th className="px-3 py-2">Port</th>
                  <th className="px-3 py-2">Bind</th>
                  <th className="px-3 py-2">URL</th>
                  <th className="px-3 py-2">Started</th>
                </tr>
              </thead>
              <tbody>
                {filteredServices.map((s) => (
                  <tr key={s.id} className="border-t border-border/60">
                    <td className="px-3 py-2">
                      <span className={`pill text-tx-xs ${sourcePillColor(s.source)}`}>
                        {s.source}
                      </span>
                    </td>
                    <td
                      className="max-w-[20rem] truncate px-3 py-2 font-mono text-tx-xs"
                      title={s.label}
                    >
                      {s.label}
                    </td>
                    <td className="px-3 py-2 font-mono text-tx-xs">
                      {s.pid ?? "—"}
                    </td>
                    <td className="px-3 py-2 font-mono text-tx-xs">
                      {s.ports && s.ports.length > 0
                        ? s.ports.join(", ")
                        : s.port ?? "—"}
                    </td>
                    <td className="px-3 py-2 font-mono text-tx-xs text-muted">
                      {s.bind_addr ?? "—"}
                    </td>
                    <td
                      className="max-w-[16rem] truncate px-3 py-2 font-mono text-tx-xs"
                      title={s.url ?? undefined}
                    >
                      {s.url ? (
                        s.url.startsWith("http") ? (
                          <a
                            href={s.url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-accent hover:underline"
                          >
                            {s.url}
                          </a>
                        ) : (
                          <span className="text-muted">{s.url}</span>
                        )
                      ) : "—"}
                    </td>
                    <td className="px-3 py-2 text-tx-xs text-muted">
                      {new Date(s.started_at * 1000).toLocaleTimeString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ───── Agent-spawned processes (per session) ─────────────────── */}
      <section className="space-y-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
            Agent-spawned processes
          </h2>
          <div className="flex items-center gap-3">
            {procs.some((p) => !p.is_alive) && (
              <button
                type="button"
                onClick={purgeDead}
                className="rounded-md border border-warning/40 bg-warning/10 px-2 py-1 text-tx-xs font-medium text-warning hover:border-warning/60"
                title="Drop DB rows for PIDs that are no longer running"
              >
                Purge {procs.filter((p) => !p.is_alive).length} dead
              </button>
            )}
            <span className="text-tx-xs text-subtle">
              auto-refreshes every 3s · {procs.length} tracked
            </span>
          </div>
        </div>
        <p className="text-tx-xs text-muted">
          Long-running processes the agent launched inside a chat session
          (npm run dev, vite preview, etc). Killed automatically when the
          parent session is deleted.
        </p>
        {procs.length === 0 ? (
          <div className="glass-card-soft p-6 text-center text-sm text-muted">
            No agent-spawned processes right now.
          </div>
        ) : (
          <div className="max-h-[60vh] overflow-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 z-10 bg-elevated text-tx-xs uppercase tracking-wide text-subtle shadow-[0_1px_0_0_rgba(0,0,0,0.08)]">
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
                  <tr
                    key={p.pid}
                    className={`border-t border-border/60 ${p.is_alive ? "" : "opacity-50"}`}
                    title={p.is_alive ? "" : "Process is no longer running (stale DB row)"}
                  >
                    <td className="px-3 py-2 font-mono text-tx-xs">
                      {p.pid}
                      {!p.is_alive && (
                        <span className="ml-1.5 text-warning" title="Process is dead">💀</span>
                      )}
                    </td>
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
                        onClick={() => kill(p.pid, p.command)}
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

      {/* ───── Users ────────────────────────────────────────────────── */}
      <section className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
            Users
          </h2>
          <span className="text-tx-xs text-subtle">
            auto-refreshes every 10s · {users.length} {users.length === 1 ? "account" : "accounts"}
          </span>
        </div>
        {users.length === 0 ? (
          <div className="glass-card-soft p-6 text-center text-sm text-muted">
            No accounts yet.
          </div>
        ) : (
          <div className="max-h-[60vh] overflow-auto rounded-xl border border-border">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 z-10 bg-elevated text-tx-xs uppercase tracking-wide text-subtle shadow-[0_1px_0_0_rgba(0,0,0,0.08)]">
                <tr>
                  <th className="px-3 py-2">Email</th>
                  <th className="px-3 py-2">Role</th>
                  <th className="px-3 py-2">Created</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  // The last root user is undeletable (server enforces too,
                  // but we hide the button so the user doesn't even try).
                  const isLastRoot =
                    u.role === "root" &&
                    users.filter((x) => x.role === "root").length <= 1;
                  // me is narrowed to AuthUser by the early returns above.
                  const isSelf = u.id === me.id;
                  return (
                    <tr key={u.id} className="border-t border-border/60">
                      <td className="px-3 py-2 font-mono text-tx-xs">
                        {u.email}
                        {isSelf && (
                          <span className="ml-2 text-tx-xs text-muted">(you)</span>
                        )}
                      </td>
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
                      <td className="px-3 py-2 text-right">
                        <div className="inline-flex items-center gap-1.5">
                          {/* Reset password — key icon, neutral */}
                          <button
                            type="button"
                            onClick={() => resetUserPassword(u)}
                            className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-elevated text-text transition-colors hover:border-accent hover:text-accent"
                            title="Reset password (invalidates their existing sessions)"
                            aria-label={`Reset password for ${u.email}`}
                          >
                            <KeyIcon className="h-4 w-4" />
                          </button>
                          {/* Delete — trash icon, red */}
                          <button
                            type="button"
                            onClick={() => deleteUser(u)}
                            disabled={isLastRoot || isSelf}
                            className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-danger/30 bg-danger/10 text-danger transition-colors hover:border-danger/50 hover:bg-danger/15 disabled:cursor-not-allowed disabled:opacity-40"
                            title={
                              isLastRoot
                                ? "Refusing to delete the last root user"
                                : isSelf
                                ? "You can't delete your own account from here"
                                : "Delete this user (kills their running processes, removes their projects + sessions)"
                            }
                            aria-label={`Delete ${u.email}`}
                          >
                            <TrashIcon className="h-4 w-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
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
            auto-refreshes every 10s · {apps.length} {apps.length === 1 ? "app" : "apps"} live
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
            No apps deployed yet. Build something in a chat session, then click the 🚀 Deploy button above the chat.
          </div>
        ) : (
          <div className="mt-3 max-h-[60vh] overflow-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="sticky top-0 z-10 bg-elevated text-left text-tx-xs uppercase tracking-wider text-muted shadow-[0_1px_0_0_rgba(0,0,0,0.08)]">
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

function sourcePillColor(source: string): string {
  switch (source) {
    case "ojas-main":      return "pill-accent";
    case "ojas-proxy":     return "";
    case "ojas-deployed":  return "";
    case "ojas-mcp":       return "";
    case "ojas-external":  return "border-warning/40 bg-warning/10 text-warning";
    case "agent-spawned":  return "";
    default:               return "";
  }
}

// (icons imported from @/components/icons)
