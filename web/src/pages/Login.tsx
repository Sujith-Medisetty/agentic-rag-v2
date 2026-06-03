import { useState } from "react";
import { authApi } from "@/lib/api";
import { setToken } from "@/lib/auth";

const defaultLabel = () => {
  const ua = navigator.userAgent;
  if (/iPhone|iPad/i.test(ua)) return "iOS";
  if (/Android/i.test(ua)) return "Android";
  if (/Macintosh/i.test(ua)) return "Mac";
  if (/Windows/i.test(ua)) return "Windows";
  return "browser";
};

export default function Login({ onDone }: { onDone: () => void }) {
  const [pass, setPass] = useState("");
  const [label, setLabel] = useState(defaultLabel());
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const { token } = await authApi.login(pass, label);
      setToken(token);
      onDone();
    } catch (e: any) {
      setErr(e?.message ?? "wrong passcode");
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
          <h1 className="text-xl font-semibold tracking-tight">Welcome back</h1>
          <p className="text-xs text-muted">Enter your passcode to continue.</p>
        </div>

        <input
          type="password"
          autoFocus
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          placeholder="Passcode"
          className="field"
        />
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Device name (optional)"
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
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
