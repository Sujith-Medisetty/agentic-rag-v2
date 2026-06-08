// DeleteProgressModal — a per-step checklist for async session/project
// deletes. Modeled on the DeployModal in pages/ChatPage.tsx:2332 so the
// visual language is identical (✓ / spinner / ✗ / ○ per step, line-through
// on done, danger-red on failed). The server walks a fixed 7-step cleanup
// (cancel_agent → kill_processes → teardown_subprojects → rmtree_subdir →
// drop_checkpoint → clear_bus → drop_rows); for a project delete the list
// is 7 * N where N is the number of sessions.
//
// The sidebar removes the entry optimistically the moment the user
// confirms — this modal is purely the "what's happening on the server
// right now" view. If the job fails, the caller is responsible for
// re-adding the entry to the sidebar (we don't reach back into the
// sidebar store from here — that would couple this component to the
// specific store the caller is using).

import { useEffect, useRef, useState } from "react";
import {
  type DeleteJobStatus,
  type DeleteStep,
  type DeleteStepStatus,
  sessionApi,
  projectsApi,
} from "@/lib/api";

// Per-session step names in the same order as the server's
// _DELETE_STEP_NAMES (server/app.py). Used for the fallback label array
// before the first poll lands AND for displaying the per-step name on
// hover. Must stay in lockstep with the server.
const STEP_LABELS: Record<string, string> = {
  cancel_agent: "Cancelling agent",
  kill_processes: "Killing spawned processes",
  teardown_subprojects: "Tearing down sub-projects",
  rmtree_subdir: "Removing workspace files",
  drop_checkpoint: "Dropping agent checkpoint",
  clear_bus: "Clearing event bus",
  drop_rows: "Removing database rows",
};

const FALLBACK_LABELS = [
  "Cancelling agent",
  "Killing spawned processes",
  "Tearing down sub-projects",
  "Removing workspace files",
  "Dropping agent checkpoint",
  "Clearing event bus",
  "Removing database rows",
];

export interface DeleteProgressModalProps {
  open: boolean;
  targetKind: "session" | "project";
  targetId: string;
  // Friendly name to show in the header ("Deleting session 'My Chat'").
  targetName: string;
  // The job metadata we got from the POST /delete 202 response. We
  // render the step list from this immediately, before the first poll
  // lands, so the user sees the full checklist the moment the modal
  // opens.
  job: { job_id: string; steps: DeleteStep[] } | null;
  // Poll a status endpoint. Caller passes either sessionApi.deleteJobStatus
  // or projectsApi.deleteJobStatus depending on targetKind — keeps the
  // modal free of branchy route knowledge.
  onPoll: () => Promise<DeleteJobStatus>;
  onCancelJob?: () => Promise<{ ok: boolean; reason?: string }>;
  onClose: () => void;
  // Subtitle the caller wants shown under the header (e.g. "this will
  // tear down 3 sub-projects across 2 sessions"). Optional.
  subtitle?: string;
}

export default function DeleteProgressModal({
  open,
  targetKind,
  targetId,
  targetName,
  job,
  onPoll,
  onCancelJob,
  onClose,
  subtitle,
}: DeleteProgressModalProps) {
  const [status, setStatus] = useState<DeleteJobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const pollHandleRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const jobIdRef = useRef<string | null>(null);

  // Reset state when the modal opens with a new job.
  useEffect(() => {
    if (open && job) {
      jobIdRef.current = job.job_id;
      // Seed the status with the job's initial step list so the UI
      // renders immediately.
      setStatus({
        job_id: job.job_id,
        target_id: targetId,
        target_kind: targetKind,
        status: "pending",
        phase: "queued",
        steps: job.steps,
        error: null,
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        completed_at: null,
      });
      setError(null);
    } else if (!open) {
      jobIdRef.current = null;
      setStatus(null);
    }
  }, [open, job?.job_id, targetId, targetKind]);

  // Poll loop. 800ms cadence matches the deploy modal so the UI feels
  // consistent. Stops on any terminal status.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled || !jobIdRef.current) return;
      try {
        const s = await onPoll();
        if (cancelled) return;
        setStatus(s);
        if (s.status === "pending" || s.status === "running") {
          pollHandleRef.current = setTimeout(tick, 800);
        }
      } catch (e: any) {
        if (cancelled) return;
        setError(e?.message ?? "status poll failed");
        // Retry after a longer delay on error so we don't hot-loop.
        pollHandleRef.current = setTimeout(tick, 2500);
      }
    };
    // Kick off the first poll after a short delay so the modal can
    // paint the initial pending state first.
    pollHandleRef.current = setTimeout(tick, 200);
    return () => {
      cancelled = true;
      if (pollHandleRef.current) clearTimeout(pollHandleRef.current);
    };
  }, [open, onPoll]);

  if (!open) return null;

  const isRunning = status?.status === "pending" || status?.status === "running";
  const isTerminal = status?.status === "succeeded" || status?.status === "failed" || status?.status === "cancelled";
  const stepCount = status?.steps.length ?? job?.steps.length ?? 0;

  const onCancel = async () => {
    if (!onCancelJob || cancelling) return;
    setCancelling(true);
    try {
      await onCancelJob();
    } catch {
      // Best-effort. The next poll will see whatever state the worker
      // reached. Don't surface this error to the user — cancellation
      // is a "please stop" request, not a critical action.
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      role="dialog"
      aria-modal="true"
      data-testid="delete-progress-modal"
    >
      <div className="w-full max-w-lg rounded-lg border border-border bg-surface p-5 shadow-lg">
        <header className="space-y-1">
          <h2 className="font-serif text-lg font-semibold text-text">
            Deleting {targetKind} <span className="font-mono text-base">"{targetName}"</span>
          </h2>
          <p className="text-tx-xs text-muted">
            {isRunning
              ? `${status?.phase || "Starting…"} — sidebar entry has already been removed.`
              : isTerminal
              ? "Done. You can close this dialog."
              : subtitle || "Server-side teardown in progress."}
            {stepCount > 0 && (
              <>
                {" "}
                <span className="text-subtle">
                  ({stepCount} step{stepCount === 1 ? "" : "s"})
                </span>
              </>
            )}
          </p>
        </header>

        <ol
          className="mt-4 max-h-[60vh] space-y-1.5 overflow-y-auto pr-1"
          data-testid="delete-steps"
        >
          {(status?.steps ?? job?.steps ?? []).map((s, idx) => {
            const status: DeleteStepStatus = s.status;
            const isCurrent = status === "running";
            const isDone_ = status === "done";
            const isFailed_ = status === "failed";
            // Fallback label: if the server hasn't populated the step
            // name yet (the 202 response seeds them with the 7-cycle
            // names but the order is a function of N sessions), use
            // the cycle index.
            const fallback = FALLBACK_LABELS[idx % FALLBACK_LABELS.length];
            const label = s.label || STEP_LABELS[s.name] || fallback;
            return (
              <li key={idx} className="flex items-start gap-2 text-tx-sm">
                <span
                  className={
                    "mt-0.5 inline-flex size-4 shrink-0 items-center justify-center font-mono " +
                    (isDone_ ? "text-success" :
                     isFailed_ ? "text-danger" :
                     isCurrent ? "text-accent" :
                     "text-subtle")
                  }
                  aria-hidden
                >
                  {isDone_ ? "✓" :
                   isFailed_ ? "✗" :
                   isCurrent ? (
                     <span className="inline-block size-3 animate-spin rounded-full border-2 border-accent border-t-transparent" />
                   ) : "○"}
                </span>
                <div className="flex-1">
                  <div
                    className={
                      isCurrent ? "font-medium text-text" :
                      isDone_ ? "text-muted line-through opacity-70" :
                      isFailed_ ? "text-danger" :
                      "text-subtle"
                    }
                  >
                    {label}
                  </div>
                  {s.message && (
                    <div
                      className={
                        "mt-0.5 text-tx-xs " + (isFailed_ ? "text-danger" : "text-muted")
                      }
                    >
                      {s.message}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>

        {error && (
          <p className="mt-3 text-tx-xs text-danger" data-testid="delete-error">
            {error}
          </p>
        )}
        {status?.error && (
          <p className="mt-3 text-tx-xs text-danger" data-testid="delete-step-error">
            {status.error}
          </p>
        )}

        <footer className="mt-5 flex items-center justify-end gap-2">
          {isRunning && onCancelJob && (
            <button
              type="button"
              onClick={onCancel}
              disabled={cancelling}
              className="rounded-md border border-border px-3 py-1.5 text-tx-sm text-muted hover:border-danger/40 hover:text-danger disabled:opacity-50"
              data-testid="delete-cancel"
            >
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="rounded-md bg-accent px-3 py-1.5 text-tx-sm font-medium text-on-accent hover:opacity-90"
            data-testid="delete-close"
          >
            {isRunning ? "Run in background" : isTerminal ? "Close" : "Close"}
          </button>
        </footer>
      </div>
    </div>
  );
}
