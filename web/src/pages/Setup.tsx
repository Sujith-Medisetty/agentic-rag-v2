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
        className="w-full max-w-sm space-y-4 rounded-lg border border-border bg-surface p-6"
      >
        <h1 className="text-xl font-semibold">Set your passcode</h1>
        <p className="text-sm text-muted">
          You'll use this to sign in from any device. Pick something you'll remember
          — there's no recovery (this is your local backend).
        </p>
        <input
          type="password"
          autoFocus
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          placeholder="New passcode"
          className="w-full rounded border border-border bg-elevated px-3 py-2 outline-none focus:border-accent"
        />
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="Confirm passcode"
          className="w-full rounded border border-border bg-elevated px-3 py-2 outline-none focus:border-accent"
        />
        {err && <div className="text-sm text-danger">{err}</div>}
        <button
          type="submit"
          disabled={busy}
          className="min-h-touch w-full rounded bg-accent px-3 py-2 font-medium text-bg disabled:opacity-50"
        >
          {busy ? "Setting up…" : "Continue"}
        </button>
      </form>
    </div>
  );
}
