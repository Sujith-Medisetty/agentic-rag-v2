// First-boot setup. Same shape as Login (signup mode), shown by AuthGate
// when /api/auth/status reports `needs_setup: true` — i.e. no users exist
// AND no root credentials are configured in .env. The first account
// created here is a regular user; if you want to be root, configure
// FORGE_ROOT_EMAIL / FORGE_ROOT_PASSWORD in .env and log in with those
// credentials instead.

import { useState } from "react";
import { authApi } from "@/lib/api";
import { setToken } from "@/lib/auth";

export default function Setup({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (password.length < 6) return setErr("Password must be at least 6 characters");
    if (password !== confirm) return setErr("Passwords don't match");
    setBusy(true);
    try {
      const { token } = await authApi.signup(email, password);
      setToken(token);
      onDone();
    } catch (e: any) {
      setErr(e?.message ?? "setup failed");
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
              Forge
            </span>
          </div>
          <h1 className="text-xl font-semibold tracking-tight">Create your account</h1>
          <p className="text-xs text-muted">
            First user on this Forge instance. Pick an email and password
            you'll remember — there's no recovery on a personal backend.
          </p>
        </div>

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
          placeholder="Password (≥ 6 characters)"
          autoComplete="new-password"
          minLength={6}
          className="field"
          required
        />
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="Confirm password"
          autoComplete="new-password"
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
          disabled={busy || !email || !password || !confirm}
          className="btn-primary min-h-touch w-full"
        >
          {busy ? "Creating account…" : "Create account"}
        </button>
      </form>
    </div>
  );
}
