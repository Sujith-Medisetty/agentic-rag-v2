// Project settings panel — inline expandable card on the SessionList page.
// Controls the Phase 4 auto-commit behaviour:
//   - auto_commit_enabled  (master switch)
//   - branch_strategy      ('session' default | 'current')
//   - auto_push_enabled    (off by default; UI nudges that this needs git
//                           auth on the user's side)
//
// Writes patch through projectsApi.updateSettings; lifts the updated Project
// row back to the parent via onChange so SessionList stays in sync.

import { useState } from "react";
import { projectsApi } from "@/lib/api";
import type { Project, BranchStrategy } from "@/lib/types";

export default function ProjectSettings({
  project, onChange,
}: { project: Project; onChange: (updated: Project) => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const patch = async (body: Partial<Project>) => {
    setBusy(true);
    setErr(null);
    try {
      const updated = await projectsApi.updateSettings(project.id, body);
      onChange(updated);
    } catch (e: any) {
      setErr(e?.message ?? "update failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4 rounded-lg border border-border bg-surface p-4">
      <div className="text-sm font-medium">Settings</div>

      <Toggle
        label="Auto-commit on each turn"
        hint="Commit anything the agent changed at the end of every assistant turn."
        checked={project.auto_commit_enabled}
        disabled={busy}
        onChange={(v) => patch({ auto_commit_enabled: v })}
      />

      <Select
        label="Branch"
        hint={
          project.branch_strategy === "session"
            ? "Each session gets its own branch (session/<id>). Safe to throw away."
            : "Commit on whichever branch is currently checked out."
        }
        value={project.branch_strategy}
        disabled={busy || !project.auto_commit_enabled}
        options={[
          { value: "session", label: "session/<id> (recommended)" },
          { value: "current", label: "current branch" },
        ]}
        onChange={(v) => patch({ branch_strategy: v as BranchStrategy })}
      />

      <Toggle
        label="Auto-push to remote"
        hint="After each commit, push to origin. Requires git push auth (SSH key or token) set up on this machine."
        checked={project.auto_push_enabled}
        disabled={busy || !project.auto_commit_enabled}
        onChange={(v) => patch({ auto_push_enabled: v })}
      />

      {err && <div className="text-sm text-danger">{err}</div>}
    </div>
  );
}

function Toggle({
  label, hint, checked, disabled, onChange,
}: {
  label: string; hint: string; checked: boolean; disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className={`flex items-start gap-3 ${disabled ? "opacity-50" : ""}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 h-4 w-4 cursor-pointer"
      />
      <div className="flex-1 text-sm">
        <div className="font-medium">{label}</div>
        <div className="text-xs text-muted">{hint}</div>
      </div>
    </label>
  );
}

function Select({
  label, hint, value, disabled, options, onChange,
}: {
  label: string; hint: string; value: string; disabled?: boolean;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div className={disabled ? "opacity-50" : ""}>
      <div className="mb-1 text-sm font-medium">{label}</div>
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="min-h-touch w-full rounded border border-border bg-elevated px-3 py-2 text-sm outline-none focus:border-accent"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <div className="mt-1 text-xs text-muted">{hint}</div>
    </div>
  );
}
