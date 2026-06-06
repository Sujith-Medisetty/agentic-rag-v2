// Shared session store — production pattern for keeping the sidebar
// in sync with chat-page state changes.
//
// Problem this solves: a chat session's name can change in three
// places (user inline rename, server auto-suffix on collision, LLM
// background auto-rename). The sidebar that lists all sessions needs
// to reflect the new name immediately, without polling, without
// window-level custom events, and across multiple route trees
// (Workspace sidebar + the SessionList page at /p/:id/sessions).
//
// Production-standard fix: lift the sessions list into a React Context
// that lives ABOVE the router. Both Workspace and SessionList consume
// it; both the chat page's WS handler and the inline rename write to
// it. The sidebar re-renders naturally when the context value changes.
//
// One source of truth. No polling. No custom DOM events. Just React.

import {
  createContext, useCallback, useContext, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import type { Session } from "@/lib/types";

interface SessionStoreValue {
  // Read the current cached list for a project. Returns [] if not yet
  // loaded. The sidebar shows what it has (with a loading hint if you
  // care); the actual fetch happens on mount of the page that needs it.
  list: (projectId: string) => Session[];
  // Replace the entire list for a project (used by initial load + refetch).
  setAll: (projectId: string, list: Session[]) => void;
  // Optimistically rename a session. Triggers a re-render in any
  // consumer that displays this session (sidebar, session list page,
  // etc.). If the new name collides, the SERVER returns the actual
  // final name (possibly with -2/-3 suffix); the caller can update
  // again with that authoritative name.
  rename: (sessionId: string, newName: string) => void;
  // Add a brand-new session to the cache (for newly-created sessions
  // that should appear at the top of the sidebar immediately).
  add: (projectId: string, session: Session) => void;
  // Remove a session (used on delete).
  remove: (sessionId: string) => void;
  // A transient toast string. Set by rename when the server auto-
  // suffixed; cleared after a few seconds. Rendered by the Layout.
  toast: { message: string } | null;
  setToast: (t: { message: string } | null) => void;
}

const SessionContext = createContext<SessionStoreValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  // Sessions keyed by project_id. We key by project rather than
  // maintaining a flat list because the chat page belongs to one
  // project at a time, and the sidebar can switch projects without
  // a remount.
  const [byProject, setByProject] = useState<Record<string, Session[]>>({});
  const [toast, setToast] = useState<{ message: string } | null>(null);
  // Auto-dismiss the toast after 4s.
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showToast = useCallback((t: { message: string } | null) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(t);
    if (t) {
      toastTimer.current = setTimeout(() => setToast(null), 4000);
    }
  }, []);

  const value = useMemo<SessionStoreValue>(() => ({
    list: (projectId) => byProject[projectId] ?? [],
    setAll: (projectId, list) => {
      setByProject((cur) => ({ ...cur, [projectId]: list }));
    },
    rename: (sessionId, newName) => {
      setByProject((cur) => {
        const next: Record<string, Session[]> = {};
        for (const [pid, arr] of Object.entries(cur)) {
          next[pid] = arr.map((s) =>
            s.id === sessionId ? { ...s, name: newName } : s,
          );
        }
        return next;
      });
    },
    add: (projectId, session) => {
      setByProject((cur) => {
        const arr = cur[projectId] ?? [];
        // De-dupe: if a session with this id is already there, replace it
        // in place; otherwise prepend.
        const without = arr.filter((s) => s.id !== session.id);
        return { ...cur, [projectId]: [session, ...without] };
      });
    },
    remove: (sessionId) => {
      setByProject((cur) => {
        const next: Record<string, Session[]> = {};
        for (const [pid, arr] of Object.entries(cur)) {
          next[pid] = arr.filter((s) => s.id !== sessionId);
        }
        return next;
      });
    },
    toast,
    setToast: showToast,
  }), [byProject, toast, showToast]);

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSessions(): SessionStoreValue {
  const v = useContext(SessionContext);
  if (!v) {
    throw new Error("useSessions must be called inside <SessionProvider>");
  }
  return v;
}
