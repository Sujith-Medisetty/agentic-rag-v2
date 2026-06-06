// Aggressive-but-legal PWA install affordance.
//
// Renders nothing if the app is already installed (display-mode: standalone).
// Otherwise:
//   - Android Chrome / Edge / desktop Chromium: tap → native install dialog
//     fires immediately. The browser still gates the underlying event on its
//     engagement heuristic, but our UI ALWAYS shows so the user knows install
//     is even an option.
//   - iOS Safari: tap → instruction sheet (Share icon → Add to Home Screen),
//     since Apple ships no install API.
//   - Browsers that haven't fired `beforeinstallprompt` yet: tap → falls back
//     to a short hint telling the user where the browser's own install
//     option lives.

import { useEffect, useState } from "react";
import {
  canPromptInstall, isIOS, isStandalone, shouldOfferInstall,
  subscribe, triggerInstall,
} from "@/lib/installPrompt";

export default function InstallButton({
  variant = "primary",
}: {
  /** "primary" — bold sidebar/CTA button.
   *  "ghost"   — subtle inline link used in less prominent surfaces. */
  variant?: "primary" | "ghost";
}) {
  const [, force] = useState(0);
  const [showIosSheet, setShowIosSheet] = useState(false);
  const [showFallback, setShowFallback] = useState(false);

  useEffect(() => subscribe(() => force((n) => n + 1)), []);

  // Renders nothing once the app is installed / running standalone — no nag.
  if (!shouldOfferInstall()) return null;

  const handleClick = async () => {
    if (isIOS()) {
      setShowIosSheet(true);
      return;
    }
    if (canPromptInstall()) {
      await triggerInstall();
      return;
    }
    // Chromium hasn't fired the deferred event yet (the engagement gate
    // hasn't been satisfied OR the user is in a fresh profile). Show a
    // short, actionable hint instead of doing nothing.
    setShowFallback(true);
  };

  const className =
    variant === "primary"
      ? "flex w-full items-center justify-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-sm font-medium text-accent transition-colors hover:bg-accent/15 hover:border-accent/60"
      : "inline-flex items-center gap-1.5 text-tx-xs text-accent hover:underline";

  return (
    <>
      <button type="button" onClick={handleClick} className={className}>
        <DownloadIcon />
        <span>Install Ojas</span>
      </button>
      {showIosSheet && <IosInstallSheet onClose={() => setShowIosSheet(false)} />}
      {showFallback && <BrowserHintSheet onClose={() => setShowFallback(false)} />}
    </>
  );
}

// ── iOS Safari instructions ───────────────────────────────────────────────

function IosInstallSheet({ onClose }: { onClose: () => void }) {
  return (
    <SheetBackdrop onClose={onClose}>
      <div className="space-y-3">
        <h3 className="font-serif text-xl font-semibold tracking-tight">
          Install Ojas on iPhone
        </h3>
        <p className="text-sm text-muted">
          Safari doesn't show a one-tap install button. Three quick steps:
        </p>
        <ol className="space-y-2 text-sm">
          <li className="flex items-baseline gap-2">
            <span className="font-mono text-accent">1.</span>
            <span>
              Tap the <strong>Share</strong> icon{" "}
              <ShareIcon className="inline h-4 w-4 -translate-y-0.5 align-middle text-accent" />{" "}
              at the bottom of Safari.
            </span>
          </li>
          <li className="flex items-baseline gap-2">
            <span className="font-mono text-accent">2.</span>
            <span>
              Scroll and tap <strong>Add to Home Screen</strong>.
            </span>
          </li>
          <li className="flex items-baseline gap-2">
            <span className="font-mono text-accent">3.</span>
            <span>
              Tap <strong>Add</strong> in the top-right corner.
            </span>
          </li>
        </ol>
        <p className="text-tx-xs text-subtle">
          Once installed, Ojas opens fullscreen from your home screen — no
          browser address bar, no tabs. Updates apply automatically.
        </p>
        <div className="flex justify-end pt-1">
          <button onClick={onClose} className="btn-ghost">Got it</button>
        </div>
      </div>
    </SheetBackdrop>
  );
}

// ── Browser hint when deferred event hasn't fired yet ─────────────────────

function BrowserHintSheet({ onClose }: { onClose: () => void }) {
  return (
    <SheetBackdrop onClose={onClose}>
      <div className="space-y-3">
        <h3 className="font-serif text-xl font-semibold tracking-tight">
          Install Ojas
        </h3>
        <p className="text-sm text-muted">
          Your browser will offer install on its own schedule. Until then, you
          can install manually:
        </p>
        <ul className="space-y-2 text-sm">
          <li className="flex items-baseline gap-2">
            <span className="text-accent">•</span>
            <span>
              <strong>Chrome / Edge (desktop)</strong>: look for the install
              icon{" "}
              <span className="inline-block rounded border border-border px-1 font-mono text-tx-xs">
                ⊕
              </span>{" "}
              in the address bar, OR open the three-dot menu →{" "}
              <strong>Install app</strong>.
            </span>
          </li>
          <li className="flex items-baseline gap-2">
            <span className="text-accent">•</span>
            <span>
              <strong>Chrome (Android)</strong>: three-dot menu →{" "}
              <strong>Add to Home screen</strong>.
            </span>
          </li>
          <li className="flex items-baseline gap-2">
            <span className="text-accent">•</span>
            <span>
              <strong>Firefox / Safari (desktop)</strong>: PWA install isn't
              supported. Use Chrome or Edge.
            </span>
          </li>
        </ul>
        <div className="flex justify-end pt-1">
          <button onClick={onClose} className="btn-ghost">Got it</button>
        </div>
      </div>
    </SheetBackdrop>
  );
}

// ── Shared modal shell ────────────────────────────────────────────────────

function SheetBackdrop({
  children, onClose,
}: { children: React.ReactNode; onClose: () => void }) {
  return (
    <div
      role="dialog"
      aria-modal
      onClick={onClose}
      className="fixed inset-0 z-40 flex items-end justify-center bg-black/45 backdrop-blur-sm sm:items-center"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-t-2xl border border-border bg-surface p-5 shadow-lift sm:rounded-2xl"
      >
        {children}
      </div>
    </div>
  );
}

// ── Inline SVG glyphs (no extra deps) ─────────────────────────────────────

function DownloadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
    </svg>
  );
}
function ShareIcon({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none"
      stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M16 6l-4-4-4 4M12 2v13M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />
    </svg>
  );
}

// Re-export utility so any other component can hide UI when standalone.
export { isStandalone };
