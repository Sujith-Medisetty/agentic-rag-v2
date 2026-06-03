// Decides on mount whether to render Setup, Login, or pass through to children
// (when a valid token is already in localStorage).

import { useEffect, useState } from "react";
import { authApi } from "@/lib/api";
import { hasToken } from "@/lib/auth";
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
        } else if (hasToken()) {
          setGate("ready");
        } else {
          setGate("login");
        }
      } catch {
        if (!cancelled) setGate("login");
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
