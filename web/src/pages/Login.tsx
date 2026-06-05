import { useEffect, useState } from "react";
import { authApi } from "@/lib/api";
import { setToken } from "@/lib/auth";

export default function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [signupAllowed, setSignupAllowed] = useState<boolean>(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Decide whether to show "Sign up" tab based on the server's policy.
  useEffect(() => {
    authApi.status()
      .then((s) => setSignupAllowed(!!s.signup_allowed))
      .catch(() => setSignupAllowed(true));
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const res = mode === "login"
        ? await authApi.login(email, password)
        : await authApi.signup(email, password);
      setToken(res.token);
      // Hard reload so every in-memory React state (Workspace's cached
      // user/projects/sessions, ChatPage's cached turns, etc.) is reset
      // and re-fetches with the new token. Without this, switching
      // identity inside one tab (root → fresh signup → "user") leaves
      // the previous user's identity stuck in component state and the
      // UI shows the wrong role + "project not found" because the
      // cached default-project belonged to the prior user.
      window.location.assign("/");
    } catch (e: any) {
      setErr(e?.message ?? (mode === "login" ? "wrong email or password" : "signup failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="glass-card w-full max-w-sm space-y-5 p-7"
      >
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span
              aria-hidden
              className="inline-block h-2.5 w-2.5 rounded-full bg-accent-gradient"
              style={{ boxShadow: "0 0 0 4px hsl(var(--accent) / 0.18)" }}
            />
            <span className="brand-mark text-sm font-semibold tracking-tight">
              Ojas
            </span>
          </div>
          <h1 className="text-xl font-semibold tracking-tight">
            {mode === "login" ? "Welcome back" : "Create an account"}
          </h1>
          <p className="text-xs text-muted">
            {mode === "login"
              ? "Enter your email and password to continue."
              : "Sign up with an email and password."}
          </p>
        </div>

        {/* Tabs — only show signup tab if the server allows it. */}
        {signupAllowed && (
          <div className="flex rounded-lg border border-border bg-elevated p-0.5">
            <button
              type="button"
              onClick={() => { setMode("login"); setErr(null); }}
              className={`flex-1 rounded-md px-3 py-1 text-sm transition-colors ${
                mode === "login" ? "bg-surface text-text shadow-soft" : "text-muted"
              }`}
            >
              Log in
            </button>
            <button
              type="button"
              onClick={() => { setMode("signup"); setErr(null); }}
              className={`flex-1 rounded-md px-3 py-1 text-sm transition-colors ${
                mode === "signup" ? "bg-surface text-text shadow-soft" : "text-muted"
              }`}
            >
              Sign up
            </button>
          </div>
        )}

        <input
          type="email"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@example.com"
          autoComplete="email"
          className="field"
          required
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoComplete={mode === "login" ? "current-password" : "new-password"}
          minLength={mode === "signup" ? 6 : 1}
          className="field"
          required
        />
        {err && (
          <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {err}
          </div>
        )}
        <button
          type="submit"
          disabled={busy || !email || !password}
          className="btn-primary min-h-touch w-full"
        >
          {busy
            ? (mode === "login" ? "Signing in…" : "Creating account…")
            : (mode === "login" ? "Sign in" : "Sign up")}
        </button>
      </form>
    </div>
  );
}
