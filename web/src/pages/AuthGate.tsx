// Decides on mount whether to render Setup, Login, or pass through to children
// (when a valid token is already in localStorage).

import { useEffect, useState } from "react";
import { authApi, ApiError } from "@/lib/api";
import { hasToken, clearToken } from "@/lib/auth";
import Setup from "@/pages/Setup";
import Login from "@/pages/Login";

type Gate = "loading" | "setup" | "login" | "ready";

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [gate, setGate] = useState<Gate>("loading");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await authApi.status();
        if (cancelled) return;
        if (status.needs_setup) {
          setGate("setup");
          return;
        }
        if (!hasToken()) {
          setGate("login");
          return;
        }
        // Validate the stored token against the server before rendering the
        // workspace. A stale token (from a different account or a cleared DB)
        // would let the workspace mount and then 401 on every API call with
        // no way to recover short of a manual logout. /me is cheap and tells
        // us whether the token is still live.
        await authApi.me();
        if (cancelled) return;
        setGate("ready");
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) clearToken();
        setGate("login");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const onAuthSuccess = () => setGate("ready");

  if (gate === "loading") {
    return (
      <div className="flex h-screen items-center justify-center text-muted">
        Connecting to backend…
      </div>
    );
  }
  if (gate === "setup") return <Setup onDone={onAuthSuccess} />;
  if (gate === "login") return <Login onDone={onAuthSuccess} />;
  return <>{children}</>;
}
