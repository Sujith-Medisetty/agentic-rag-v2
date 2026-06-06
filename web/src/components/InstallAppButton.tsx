// Always-visible PWA install control. Combines:
//   1) The native browser install dialog (when beforeinstallprompt fires)
//   2) A manual "how to install" modal for browsers/devices that
//      don't fire that event (iOS Safari, Firefox, in-app browsers,
//      strict Chrome privacy settings, already-dismissed-this-session, etc.)
//
// The button is ALWAYS in the header so the user can always discover
// how to install the app. The native install path is used when
// available; otherwise the modal gives platform-specific instructions.

import { useState } from "react";
import { useInstallPWA } from "@/lib/useInstallPWA";

export function InstallAppButton() {
  const pwa = useInstallPWA();
  const [modalOpen, setModalOpen] = useState(false);

  if (pwa.installed) {
    return (
      <span
        className="inline-flex h-8 items-center gap-1 rounded-md border border-success/40 bg-success/10 px-2.5 text-xs font-medium text-success"
        title="Ojas is installed as an app"
        aria-label="Ojas is installed"
      >
        <CheckIcon />
        <span className="hidden sm:inline">Installed</span>
      </span>
    );
  }

  const onClick = async () => {
    if (pwa.hasSavedPrompt) {
      // Native install dialog — Chrome / Edge / Samsung / etc. The
      // prompt was captured at module scope (see useInstallPWA.ts)
      // so it survives re-renders, HMR, and route changes.
      await pwa.install();
      return;
    }
    // Fall back to the manual-instructions modal. Covers:
    // - iOS Safari (no beforeinstallprompt; uses Share → Add to Home Screen)
    // - Firefox / Edge strict-mode / already-dismissed-this-session
    // - In-app browsers (Twitter, Slack, etc.) where install isn't supported
    setModalOpen(true);
  };

  return (
    <>
      <button
        type="button"
        onClick={onClick}
        className="inline-flex h-8 items-center gap-1 rounded-md border border-accent/40 bg-accent/10 px-2.5 text-xs font-semibold text-accent transition-colors hover:bg-accent/20 hover:border-accent/60"
        title="Install Ojas as a desktop / home-screen app"
        aria-label="Install Ojas as an app"
      >
        <DownloadIcon />
        <span>Install app</span>
      </button>
      {modalOpen && <InstallHelpModal onClose={() => setModalOpen(false)} />}
    </>
  );
}

function InstallHelpModal({ onClose }: { onClose: () => void }) {
  const ref = (el: HTMLDialogElement | null) => {
    if (el && !el.open) el.showModal();
  };
  const ua =
    typeof navigator !== "undefined" ? navigator.userAgent : "";
  const isIos = /iPhone|iPad|iPod/i.test(ua) ||
    (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  const isAndroid = /Android/i.test(ua);
  const isFirefox = /Firefox/i.test(ua);
  const isSafari = /Safari/i.test(ua) && !/Chrome|Chromium|Edg/i.test(ua);
  const inAppBrowser = /FBAN|FBAV|Instagram|Twitter|LinkedInApp|WeChat|Line\//i.test(ua);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) (e.currentTarget as HTMLDialogElement).close();
      }}
      className="rounded-xl border border-border bg-bg p-0 text-text shadow-2xl backdrop:bg-black/40"
    >
      <div className="w-[min(92vw,32rem)] p-5">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-accent/40 bg-accent/10 text-accent">
            <DownloadIcon />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="font-serif text-lg font-semibold leading-tight">
              Install Ojas
            </h2>
            <p className="mt-1 text-sm text-muted">
              Get a one-tap home-screen / desktop app — no URL bar, no browser chrome.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-muted hover:text-text"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="mt-4 space-y-3 text-sm">
          {isIos && (
            <div className="rounded-md border border-border bg-elevated p-3">
              <div className="font-medium">📱 iPhone / iPad (Safari)</div>
              <ol className="mt-1 list-decimal pl-5 text-muted">
                <li>Tap the <b>Share</b> button (square with arrow) in the Safari toolbar.</li>
                <li>Scroll down and tap <b>"Add to Home Screen"</b>.</li>
                <li>Tap <b>Add</b>. Ojas will appear as an app icon on your home screen.</li>
              </ol>
              <p className="mt-2 text-xs text-subtle">
                iOS only allows PWA install via Share → Add to Home Screen; there's
                no browser-side install button on iOS Safari.
              </p>
            </div>
          )}

          {isAndroid && !isIos && (
            <div className="rounded-md border border-border bg-elevated p-3">
              <div className="font-medium">🤖 Android (Chrome / Edge / Samsung)</div>
              <p className="mt-1 text-muted">
                Tap the menu (⋮) → <b>"Install app"</b> or <b>"Add to Home screen"</b>.
                The browser should show an install banner automatically.
              </p>
              <p className="mt-2 text-xs text-subtle">
                If the menu doesn't show "Install app", your browser may
                have already shown the install prompt and you dismissed it.
                Clearing site data in DevTools resets that.
              </p>
            </div>
          )}

          {!isIos && !isAndroid && (
            <div className="rounded-md border border-border bg-elevated p-3">
              <div className="font-medium">🖥️ Desktop ({isFirefox ? "Firefox" : isSafari ? "Safari" : "Chrome / Edge / Brave"})</div>
              <p className="mt-1 text-muted">
                {isFirefox || isSafari ? (
                  <>Firefox and Safari don't support PWA install natively. Use
                  Chrome or Edge for the installable app experience, or
                  bookmark this page for one-click access.</>
                ) : (
                  <>Look for the <b>install icon</b> (⊕) in the right side
                  of the URL bar. Click it → <b>Install</b>. You'll get a
                  standalone Ojas app in your Applications folder or Start menu.</>
                )}
              </p>
            </div>
          )}

          {inAppBrowser && (
            <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
              <div className="font-medium">⚠️ In-app browser detected</div>
              <p className="mt-1 text-muted">
                You're viewing Ojas inside another app's browser (Twitter, Slack, etc.).
                These browsers don't support PWA install. Open this page in
                your system browser to install.
              </p>
            </div>
          )}

          <div className="rounded-md border border-border bg-elevated p-3 text-xs text-muted">
            <b>Tip:</b> PWAs work offline once installed. Your chat history
            is cached locally, so even flaky internet won't break the UI.
          </div>
        </div>

        <div className="mt-5 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="btn-ghost min-h-touch"
            autoFocus
          >
            Got it
          </button>
        </div>
      </div>
    </dialog>
  );
}

function DownloadIcon() {
  return (
    <svg
      width="14" height="14" viewBox="0 0 24 24"
      fill="none" stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round" strokeLinejoin="round"
      aria-hidden
    >
      <path d="M12 4v12M6 12l6 6 6-6M5 20h14" />
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg
      width="14" height="14" viewBox="0 0 24 24"
      fill="none" stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round" strokeLinejoin="round"
      aria-hidden
    >
      <path d="M5 12l4 4L19 6" />
    </svg>
  );
}
