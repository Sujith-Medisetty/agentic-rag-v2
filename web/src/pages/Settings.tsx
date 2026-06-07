import { useCallback, useEffect, useState } from "react";
import { deployedAppsApi } from "@/lib/api";
import type { DeployedApp, DeployedAppsBySession } from "@/lib/api";

// ============================================================
// Settings — pause/resume toggles for every deployed app the
// caller can see, grouped by source session. Root sees every
// app; non-root sees their own + orphans. Each app shows a
// state badge + a single up/down switch + an Open link +
// a Delete button. Deleting cascades the session-cleanup path
// (wipes the live and .stopped/ dirs).
//
// We re-fetch after every mutation rather than mutating local
// state; the toggle latency is sub-100ms and a stale UI is worse
// than a small roundtrip. The state badge colour comes from
// the backend's "state" field directly — no client-side guess.
// ============================================================

const STATE_META: Record<string, { dot: string; label: string; tone: string }> = {
  running:  { dot: "bg-emerald-500", label: "Running",  tone: "text-emerald-400" },
  stopped:  { dot: "bg-zinc-500",    label: "Paused",   tone: "text-zinc-400" },
  starting: { dot: "bg-amber-500",   label: "Starting", tone: "text-amber-400" },
  error:    { dot: "bg-rose-500",    label: "Error",    tone: "text-rose-400" },
};

function StateBadge({ state }: { state: string }) {
  const meta = STATE_META[state] ?? STATE_META.stopped;
  return (
    <span className={`inline-flex items-center gap-1.5 text-tx-xs font-medium ${meta.tone}`}>
      <span className={`inline-block size-2 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}

function urlFor(slug: string): string {
  // Match the apex ojas.karmacode.cloud → apps live at <slug>.<host>
  // The host is the apex with "ojas." stripped. Frontend knows this
  // because the chat strip uses the same host. We mirror that here.
  const host = window.location.hostname.replace(/^ojas\./, "");
  return `https://${slug}.${host}/`;
}

export default function Settings() {
  const [groups, setGroups] = useState<DeployedAppsBySession[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);  // slug being toggled

  const reload = useCallback(async () => {
    try {
      const data = await deployedAppsApi.mine();
      setGroups(data);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message ?? "failed to load");
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const toggle = useCallback(async (app: DeployedApp) => {
    setBusy(app.slug);
    setErr(null);
    try {
      if (app.state === "running") {
        await deployedAppsApi.stop(app.slug);
      } else if (app.state === "stopped" || app.state === "error") {
        await deployedAppsApi.start(app.slug);
      } else {
        // starting — wait, the user will retry
      }
      await reload();
    } catch (e: any) {
      setErr(e?.message ?? "toggle failed");
    } finally {
      setBusy(null);
    }
  }, [reload]);

  const remove = useCallback(async (app: DeployedApp) => {
    if (!confirm(`Delete "${app.name}"? This wipes the app dir and frees the slug permanently.`)) {
      return;
    }
    setBusy(app.slug);
    setErr(null);
    try {
      await deployedAppsApi.delete(app.slug);
      await reload();
    } catch (e: any) {
      setErr(e?.message ?? "delete failed");
    } finally {
      setBusy(null);
    }
  }, [reload]);

  if (groups === null) {
    return (
      <div className="mx-auto max-w-3xl p-6 text-muted">Loading…</div>
    );
  }

  if (groups.length === 0) {
    return (
      <div className="mx-auto max-w-3xl space-y-3 p-6">
        <h1 className="font-serif text-2xl font-semibold tracking-tight">Settings</h1>
        <div className="rounded-2xl border border-border bg-surface p-8 text-center">
          <p className="text-tx-sm text-muted">No deployed apps yet.</p>
          <p className="mt-1 text-tx-xs text-subtle">
            Build something in a chat session, then click the <span className="font-mono">🚀 Deploy</span> button above the chat to publish it. The dialog asks for a slug and shows the project to deploy.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <header>
        <h1 className="font-serif text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-tx-xs text-muted">
          Pause an app to free its memory + CPU without losing the slug. Toggle it back on
          to restore the same URL.
        </p>
      </header>

      {err && (
        <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-tx-xs text-danger">
          {err}
        </div>
      )}

      {groups.map((g) => (
        <section
          key={g.session_id ?? "_orphan"}
          className="rounded-2xl border border-border bg-surface p-4"
        >
          <h2 className="mb-3 font-serif text-lg font-semibold tracking-tight">
            {g.session_name}
            {g.session_id && (
              <span className="ml-2 font-mono text-tx-xs text-subtle">
                {g.session_id.slice(0, 8)}
              </span>
            )}
          </h2>

          <ul className="divide-y divide-border">
            {g.deployed_apps.map((app) => {
              const isBusy = busy === app.slug;
              const isOff = app.state === "stopped" || app.state === "error";
              return (
                <li
                  key={app.slug}
                  className="flex flex-wrap items-center gap-3 py-3 sm:flex-nowrap"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <a
                        href={urlFor(app.slug)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="truncate font-mono text-tx-sm font-medium text-fg hover:underline"
                      >
                        {app.slug}
                      </a>
                      <StateBadge state={app.state} />
                    </div>
                    <p className="mt-0.5 truncate text-tx-xs text-muted">
                      {app.name}
                      {app.project_dir && (
                        <span className="ml-1 text-subtle">
                          · <span className="font-mono">{app.project_dir}/</span>
                        </span>
                      )}
                    </p>
                  </div>

                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => toggle(app)}
                      disabled={isBusy || app.state === "starting"}
                      aria-label={isOff ? "Bring app online" : "Pause app"}
                      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full
                                  transition-colors disabled:opacity-50
                                  ${isOff ? "bg-zinc-600" : "bg-emerald-500"}`}
                    >
                      <span
                        className={`inline-block size-5 transform rounded-full bg-white shadow
                                    transition-transform
                                    ${isOff ? "translate-x-0.5" : "translate-x-5"}`}
                      />
                    </button>
                    <button
                      onClick={() => remove(app)}
                      disabled={isBusy}
                      className="rounded border border-border bg-bg px-2 py-1 text-tx-xs text-muted hover:border-danger/40 hover:text-danger disabled:opacity-50"
                    >
                      Delete
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
