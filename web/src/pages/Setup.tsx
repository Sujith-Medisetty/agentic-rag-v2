import { useState } from "react";
import { authApi } from "@/lib/api";
import { setToken } from "@/lib/auth";

export default function Setup({ onDone }: { onDone: () => void }) {
  const [pass, setPass] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (pass.length < 4) return setErr("Passcode must be at least 4 characters");
    if (pass !== confirm) return setErr("Passcodes don't match");
    setBusy(true);
    try {
      const { token } = await authApi.setup(pass);
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
              className="inline-block h-2.5 w-2.5 rounded-full bg-accent-gradient shadow-glow-accent"
            />
            <span className="brand-mark text-sm font-semibold tracking-tight">
              agentic&#8209;rag
            </span>
          </div>
          <h1 className="text-xl font-semibold tracking-tight">Set your passcode</h1>
          <p className="text-xs text-muted">
            You'll use this to sign in from any device. Pick something you'll
            remember — there's no recovery (this is your local backend).
          </p>
        </div>

        <input
          type="password"
          autoFocus
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          placeholder="New passcode"
          className="field"
        />
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="Confirm passcode"
          className="field"
        />
        {err && (
          <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {err}
          </div>
        )}
        <button
          type="submit"
          disabled={busy}
          className="btn-primary min-h-touch w-full"
        >
          {busy ? "Setting up…" : "Continue"}
        </button>
      </form>
    </div>
  );
}
