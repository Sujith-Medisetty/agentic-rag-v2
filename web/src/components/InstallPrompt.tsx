// "Install app" banner.
//
// Chromium browsers (Chrome/Edge/Brave on Android + desktop) fire a
// `beforeinstallprompt` event when the page meets PWA install criteria. We
// stash it and surface a custom button — the browser's built-in install
// chip is easy to miss.
//
// Safari (iOS + macOS) doesn't fire that event at all; iOS users get a
// separate hint via IosInstallHint.tsx.
//
// We hide the banner entirely once the app is running in standalone mode
// (already installed), or if the user dismisses it (remembered for 30 days).

import { useEffect, useState } from "react";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

const DISMISS_KEY = "agentic-rag.install-prompt.dismissed-at";
const DISMISS_TTL_MS = 30 * 24 * 60 * 60 * 1000;   // 30 days

function isDismissedRecently(): boolean {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return false;
    return Date.now() - parseInt(raw, 10) < DISMISS_TTL_MS;
  } catch {
    return false;
  }
}

function isStandalone(): boolean {
  // matchMedia for Chromium/Firefox; navigator.standalone for iOS Safari.
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // @ts-expect-error legacy iOS-only property
    window.navigator.standalone === true
  );
}

export default function InstallPrompt() {
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (isStandalone() || isDismissedRecently()) return;
    const onBeforeInstall = (e: Event) => {
      e.preventDefault();
      setDeferred(e as BeforeInstallPromptEvent);
      setVisible(true);
    };
    const onInstalled = () => setVisible(false);
    window.addEventListener("beforeinstallprompt", onBeforeInstall);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onBeforeInstall);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  if (!visible || !deferred) return null;

  const install = async () => {
    try {
      await deferred.prompt();
      const { outcome } = await deferred.userChoice;
      if (outcome === "dismissed") rememberDismiss();
    } finally {
      setDeferred(null);
      setVisible(false);
    }
  };

  const dismiss = () => {
    rememberDismiss();
    setVisible(false);
  };

  return (
    <div className="fixed bottom-4 left-1/2 z-30 w-[min(420px,calc(100vw-32px))] -translate-x-1/2 rounded-lg border border-border bg-elevated p-3 shadow-lg">
      <div className="flex items-start gap-3">
        <div className="flex-1 text-sm">
          <div className="font-medium">Install agentic-rag</div>
          <div className="text-muted">
            Add it to your home screen for one-tap access.
          </div>
        </div>
        <button
          onClick={dismiss}
          className="rounded border border-border px-2 py-1 text-xs"
        >
          Not now
        </button>
        <button
          onClick={install}
          className="rounded bg-accent px-3 py-1 text-xs font-medium text-white"
        >
          Install
        </button>
      </div>
    </div>
  );
}

function rememberDismiss() {
  try {
    localStorage.setItem(DISMISS_KEY, String(Date.now()));
  } catch {
    /* ignore */
  }
}
