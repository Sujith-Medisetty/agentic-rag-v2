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
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="grid w-full max-w-5xl gap-10 md:grid-cols-[1.1fr_minmax(320px,360px)] md:gap-12 md:items-center">
        {/* Hero — first thing a new visitor reads. Tells them what Ojas
            does and how to use it before they fill in the form. On mobile
            it stacks above the form; on desktop it sits to the left. */}
        <section className="space-y-6 text-center md:text-left">
          <div className="flex items-center justify-center gap-3 md:justify-start">
            <span
              aria-hidden
              className="inline-block h-3.5 w-3.5 rounded-full bg-accent-gradient"
              style={{ boxShadow: "0 0 0 6px hsl(var(--accent) / 0.18)" }}
            />
            <span className="brand-mark text-base font-semibold tracking-tight">
              Ojas
            </span>
          </div>

          <div className="space-y-3">
            <h1 className="font-serif text-4xl font-semibold tracking-tight md:text-5xl">
              Your personal coding agent.
            </h1>
            <p className="text-base text-muted md:text-lg">
              Describe what you want in plain English — Ojas plans, writes,
              runs, and ships the code for you. Apps, scripts, bug fixes,
              prototypes. It thinks like an engineer so you don't have to.
            </p>
          </div>

          <ul className="space-y-3 text-left text-sm">
            <li className="flex items-start gap-3">
              <span aria-hidden className="mt-1 text-accent">●</span>
              <span>
                <span className="font-medium text-text">Just chat.</span>{" "}
                <span className="text-muted">
                  Tell it &ldquo;build me a todo app&rdquo;, &ldquo;fix this bug&rdquo;,
                  or &ldquo;deploy a portfolio site&rdquo; — same way you'd brief a teammate.
                </span>
              </span>
            </li>
            <li className="flex items-start gap-3">
              <span aria-hidden className="mt-1 text-accent">●</span>
              <span>
                <span className="font-medium text-text">It runs the work.</span>{" "}
                <span className="text-muted">
                  Reads files, edits code, runs tests, installs deps,
                  builds, and previews the result — autonomously.
                </span>
              </span>
            </li>
            <li className="flex items-start gap-3">
              <span aria-hidden className="mt-1 text-accent">●</span>
              <span>
                <span className="font-medium text-text">Install as a PWA.</span>{" "}
                <span className="text-muted">
                  Add Ojas to your home screen on phone or desktop — it
                  feels like a native app, works offline-tolerant.
                </span>
              </span>
            </li>
          </ul>

          <p className="hidden text-xs text-muted/80 md:block">
            Built by Sujith Medisetty · self-hosted · no data leaves your VM
          </p>
        </section>

        {/* Auth card — the existing login/signup form, untouched apart from
            wrapping. Lives on the right on desktop, below the hero on mobile. */}
        <form
          onSubmit={submit}
          className="glass-card w-full space-y-5 p-7 md:max-w-sm"
        >
        <div className="space-y-1">
          <h2 className="text-xl font-semibold tracking-tight">
            {mode === "login" ? "Welcome back" : "Create an account"}
          </h2>
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
    </div>
  );
}
