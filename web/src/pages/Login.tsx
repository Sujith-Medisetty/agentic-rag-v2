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
        className="w-full max-w-sm space-y-4 rounded-lg border border-border bg-surface p-6"
      >
        <h1 className="text-xl font-semibold">Sign in</h1>
        <input
          type="password"
          autoFocus
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          placeholder="Passcode"
          className="w-full rounded border border-border bg-elevated px-3 py-2 outline-none focus:border-accent"
        />
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Device name (optional)"
          className="w-full rounded border border-border bg-elevated px-3 py-2 outline-none focus:border-accent"
        />
        {err && <div className="text-sm text-danger">{err}</div>}
        <button
          type="submit"
          disabled={busy}
          className="min-h-touch w-full rounded bg-accent px-3 py-2 font-medium text-bg disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
