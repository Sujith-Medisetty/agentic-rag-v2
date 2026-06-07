// InstallButton — capture the beforeinstallprompt event so the user
// gets a real "Install app" button instead of having to dig through
// the browser menu. The component renders nothing on:
//   - browsers that don't expose beforeinstallprompt (e.g. iOS Safari)
//   - after the user has already installed the PWA
//   - when there's no deferred prompt yet (we re-render when one arrives)
//
// Match the rest of Ojas's apps: this file is the same shape in
// every scaffold, copy it verbatim, only change the color classes
// if you themed away from indigo.

import { useEffect, useState } from "react";

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

let deferred: BeforeInstallPromptEvent | null = null;
const listeners = new Set<() => void>();
if (typeof window !== "undefined") {
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e as BeforeInstallPromptEvent;
    listeners.forEach((fn) => fn());
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    listeners.forEach((fn) => fn());
  });
}

function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  const mql = window.matchMedia("(display-mode: standalone)");
  if (mql.matches) return true;
  const nav = window.navigator as Navigator & { standalone?: boolean };
  return nav.standalone === true;
}

function isIOS(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iPad|iPhone|iPod/.test(navigator.userAgent) && !("MSStream" in window);
}

export default function InstallButton() {
  const [promptReady, setPromptReady] = useState<boolean>(!!deferred);
  const [installed, setInstalled] = useState<boolean>(isStandalone());
  const [showHint, setShowHint] = useState<null | "ios" | "other">(null);

  useEffect(() => {
    const onChange = () => {
      setPromptReady(!!deferred);
      setInstalled(isStandalone());
    };
    listeners.add(onChange);
    const mql = window.matchMedia("(display-mode: standalone)");
    mql.addEventListener("change", onChange);
    return () => {
      listeners.delete(onChange);
      mql.removeEventListener("change", onChange);
    };
  }, []);

  if (installed) return null;
  if (promptReady) {
    const click = async () => {
      if (!deferred) return;
      await deferred.prompt();
      await deferred.userChoice;
      deferred = null;
      setPromptReady(false);
    };
    return (
      <button
        type="button"
        onClick={click}
        className="inline-flex items-center gap-1.5 rounded-md border border-indigo-400/40 bg-indigo-500/10 px-3 py-1.5 text-sm font-medium text-indigo-600 hover:bg-indigo-500/15"
      >
        ↓ Install
      </button>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setShowHint(isIOS() ? "ios" : "other")}
        className="inline-flex items-center gap-1.5 rounded-md border border-indigo-400/40 bg-indigo-500/10 px-3 py-1.5 text-sm font-medium text-indigo-600 hover:bg-indigo-500/15"
      >
        ↓ Install
      </button>
      {showHint && (
        <div
          role="dialog"
          aria-modal
          className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm"
          onClick={() => setShowHint(null)}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="w-full max-w-md rounded-t-2xl sm:rounded-2xl border border-gray-200 bg-white p-5 shadow-xl"
          >
            {showHint === "ios" ? (
              <>
                <h3 className="text-lg font-semibold mb-2">Install on iPhone</h3>
                <ol className="space-y-2 text-sm">
                  <li>1. Tap the <strong>Share</strong> icon at the bottom of Safari.</li>
                  <li>2. Scroll and tap <strong>Add to Home Screen</strong>.</li>
                  <li>3. Tap <strong>Add</strong> in the top-right.</li>
                </ol>
              </>
            ) : (
              <>
                <h3 className="text-lg font-semibold mb-2">Install</h3>
                <p className="text-sm">
                  Use your browser's menu → <strong>Install app</strong> or{" "}
                  <strong>Add to Home Screen</strong>. Chrome/Edge also show
                  an install icon in the address bar.
                </p>
              </>
            )}
            <button
              onClick={() => setShowHint(null)}
              className="mt-4 rounded-md border border-gray-200 px-3 py-1.5 text-sm"
            >
              Got it
            </button>
          </div>
        </div>
      )}
    </>
  );
}
