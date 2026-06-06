// PWA diagnostics + "Reset PWA" button. Opens a modal that shows:
//   - Current display mode (browser tab vs standalone PWA)
//   - HTTPS / secure-context state
//   - Service worker registration state (the most common reason the
//     install button doesn't show is a stale SW from a prior version)
//   - Manifest reachability + contents summary
//   - Whether beforeinstallprompt has ever fired in this session
//   - One-click "Reset PWA" that unregisters the SW + clears all
//     caches, then reloads — this fixes the "dismissed install
//     prompt this session" gotcha (Chrome remembers the dismissal
//     per-origin; you have to wipe site data to re-prompt)
//
// We don't try to be clever about diagnosing the installability —
// Chrome's own DevTools "Application > Manifest" tab is the source
// of truth. This modal just makes the common cases obvious.

import { useEffect, useState } from "react";
import { useInstallPWA } from "@/lib/useInstallPWA";

interface DiagState {
  standalone: boolean;
  secureContext: boolean;
  hasManifest: boolean | null;
  manifestName: string | null;
  hasSW: boolean | null;
  swScope: string | null;
  swState: string | null;
  promptFired: boolean;
  installable: boolean;
  isIos: boolean;
  inApp: boolean;
  userAgent: string;
}

export function PWADiagnosticsButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="btn-icon"
        title="PWA diagnostics — show install state, SW status, and a reset button"
        aria-label="PWA diagnostics"
      >
        <QuestionIcon />
      </button>
      {open && <PWADiagnosticsModal onClose={() => setOpen(false)} />}
    </>
  );
}

function PWADiagnosticsModal({ onClose }: { onClose: () => void }) {
  const pwa = useInstallPWA();
  const [diag, setDiag] = useState<DiagState | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const refresh = async () => {
    setResult(null);
    const ua = navigator.userAgent;
    const isIos = /iPhone|iPad|iPod/i.test(ua) ||
      (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
    const inApp = /FBAN|FBAV|Instagram|Twitter|LinkedInApp|WeChat/i.test(ua);

    let hasManifest: boolean | null = null;
    let manifestName: string | null = null;
    try {
      const r = await fetch("/manifest.webmanifest", { cache: "no-store" });
      hasManifest = r.ok;
      if (r.ok) {
        const m = await r.json();
        manifestName = String(m.name ?? m.short_name ?? "(unnamed)");
      }
    } catch {
      hasManifest = false;
    }

    let hasSW: boolean | null = null;
    let swScope: string | null = null;
    let swState: string | null = null;
    if ("serviceWorker" in navigator) {
      try {
        const reg = await navigator.serviceWorker.getRegistration();
        hasSW = !!reg;
        if (reg) {
          swScope = reg.scope;
          const sw = reg.active || reg.installing || reg.waiting;
          if (sw) swState = sw.state;
        }
      } catch {
        hasSW = false;
      }
    }

    setDiag({
      standalone: window.matchMedia?.("(display-mode: standalone)").matches ||
        (navigator as any).standalone === true,
      secureContext: window.isSecureContext,
      hasManifest,
      manifestName,
      hasSW,
      swScope,
      swState,
      promptFired: (window as any).__pwa_prompt_seen === true,
      installable: pwa.supported,
      isIos,
      inApp,
      userAgent: ua,
    });
  };

  useEffect(() => { void refresh(); }, []);

  const resetPwa = async () => {
    setBusy(true);
    setResult("Unregistering SW + clearing caches…");
    try {
      if ("serviceWorker" in navigator) {
        const regs = await navigator.serviceWorker.getRegistrations();
        await Promise.all(regs.map((r) => r.unregister()));
      }
      if ("caches" in window) {
        const names = await caches.keys();
        await Promise.all(names.map((n) => caches.delete(n)));
      }
      // Clear our local-storage dismiss flags too
      try {
        localStorage.removeItem("agentic-rag.install-prompt.dismissed-at");
        localStorage.removeItem("agentic-rag.ios-install-hint.dismissed-at");
      } catch { /* ignore */ }
      setResult("Done. Reloading…");
      setTimeout(() => window.location.reload(), 600);
    } catch (e: any) {
      setResult(`Reset failed: ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <dialog
      open
      onClose={onClose}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      className="rounded-xl border border-border bg-bg p-0 text-text shadow-2xl backdrop:bg-black/40"
    >
      <div className="w-[min(94vw,36rem)] p-5">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-accent/40 bg-accent/10 text-accent">
            <QuestionIcon />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="font-serif text-lg font-semibold leading-tight">
              PWA diagnostics
            </h2>
            <p className="mt-1 text-sm text-muted">
              What Chrome sees when it decides whether to offer install.
            </p>
          </div>
          <button onClick={onClose} className="rounded-md px-2 py-1 text-muted hover:text-text" aria-label="Close">✕</button>
        </div>

        <div className="mt-4 max-h-[60vh] overflow-y-auto pr-1">
          {!diag ? (
            <div className="py-8 text-center text-sm text-muted">Checking…</div>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                <Row label="Display mode" value={diag.standalone ? "Standalone (installed app)" : "Browser tab"} ok={diag.standalone} />
                <Row label="Secure context (HTTPS)" value={diag.secureContext ? "Yes" : "No"} ok={diag.secureContext} />
                <Row label="Manifest reachable" value={diag.hasManifest === null ? "…" : diag.hasManifest ? "Yes" : "No"} ok={diag.hasManifest === true} />
                <Row label="Manifest name" value={diag.manifestName ?? "—"} />
                <Row label="Service worker registered" value={diag.hasSW === null ? "…" : diag.hasSW ? `Yes (${diag.swState ?? "?"})` : "No"} ok={diag.hasSW === true} />
                <Row label="SW scope" value={diag.swScope ?? "—"} />
                <Row label="beforeinstallprompt fired" value={diag.promptFired ? "Yes" : "No (Chrome hasn't offered install this session)"} ok={diag.promptFired} />
                <Row label="Install button visible" value={diag.installable ? "Yes" : "No (use Reset PWA below)"} ok={diag.installable} />
                <Row label="iOS Safari" value={diag.isIos ? "Yes (use Share → Add to Home Screen)" : "No"} />
                <Row label="In-app browser" value={diag.inApp ? "Yes (open in system browser)" : "No"} />
                <tr>
                  <td className="py-1 pr-2 text-muted">User agent</td>
                  <td className="py-1 text-tx-xs font-mono break-all">{diag.userAgent}</td>
                </tr>
              </tbody>
            </table>
          )}
        </div>

        <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            className="rounded-md border border-border bg-elevated px-3 py-1.5 text-xs font-medium text-text hover:border-accent"
          >
            Refresh
          </button>
          <div className="flex flex-col items-end gap-1">
            <button
              type="button"
              onClick={resetPwa}
              disabled={busy}
              className="rounded-md bg-danger/90 px-3 py-1.5 text-xs font-semibold text-white hover:bg-danger disabled:opacity-50"
            >
              {busy ? "Resetting…" : "Reset PWA (unregister SW + clear caches)"}
            </button>
            {result && (
              <div className="text-tx-xs text-muted">{result}</div>
            )}
          </div>
        </div>

        <div className="mt-3 rounded-md border border-info/40 bg-info/10 p-2.5 text-tx-xs text-muted">
          <b>Tip:</b> for definitive answers, open DevTools →{" "}
          <span className="font-mono">Application → Manifest</span>. Chrome
          will show the exact installability failure reason there.
        </div>
      </div>
    </dialog>
  );
}

function Row({ label, value, ok }: { label: string; value: string; ok?: boolean }) {
  return (
    <tr>
      <td className="py-1 pr-2 align-top text-muted">{label}</td>
      <td className="py-1 align-top">
        <span className={ok === true ? "text-success" : ok === false ? "text-warning" : ""}>
          {value}
        </span>
      </td>
    </tr>
  );
}

function QuestionIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9a2.5 2.5 0 1 1 4 1.5c-.7.7-1.5 1-1.5 2M12 17h.01" />
    </svg>
  );
}
