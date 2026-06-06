// Single global toast renderer. Mounted ONCE at the App level so the
// toast survives route changes (e.g. it doesn't disappear if the
// rename fires while the user is navigating). Anywhere in the tree
// can show a toast by calling useSessions().setToast({...}).

import { useSessions } from "@/lib/sessionContext";

export function GlobalToast() {
  const { toast, setToast } = useSessions();
  if (!toast) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-info/40 bg-info/10 px-4 py-2 text-sm text-text shadow-lg backdrop-blur"
    >
      <span aria-hidden="true" className="mr-2">ℹ️</span>
      {toast.message}
      <button
        type="button"
        onClick={() => setToast(null)}
        className="ml-3 rounded text-xs text-muted hover:text-text"
        aria-label="Dismiss"
      >
        ✕
      </button>
    </div>
  );
}
