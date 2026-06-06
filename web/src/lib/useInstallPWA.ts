// React hook for the PWA install prompt.
//
// The browser fires `beforeinstallprompt` once the app meets install
// criteria (valid manifest, service worker, served over HTTPS). We
// capture the event so we can show our own explicit "Install" button
// in the header — Chrome's tiny browser-level install chip is easy
// to miss, especially on mobile.
//
// State:
//   - `supported`: true if the browser fires beforeinstallprompt
//     (Chrome, Edge, Android browsers, etc.) and the app is installable
//   - `installed`: true if the user has already installed the app
//     (running in standalone mode)
//   - `install()`: call to trigger the browser's install dialog
//
// iOS Safari doesn't fire beforeinstallprompt at all — those users
// get a separate hint via the IosInstallHint component (Share →
// Add to Home Screen).

import { useCallback, useEffect, useState } from "react";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export function useInstallPWA() {
  const [deferredPrompt, setDeferredPrompt] =
    useState<BeforeInstallPromptEvent | null>(null);
  const [installed, setInstalled] = useState(false);

  useEffect(() => {
    // Detect already-installed (PWA running in standalone mode).
    const isStandalone =
      typeof window !== "undefined" &&
      (window.matchMedia?.("(display-mode: standalone)").matches ||
        // iOS Safari: navigator.standalone is the legacy check
        (window.navigator as any).standalone === true);
    if (isStandalone) {
      setInstalled(true);
      console.info("[pwa] Running in standalone mode — app is already installed");
    } else {
      console.info("[pwa] Not in standalone mode — install button can show");
    }

    const onBeforeInstall = (e: Event) => {
      // Chrome requires us to preventDefault() to keep the event
      // around (otherwise Chrome shows its own banner immediately
      // and we can't show ours).
      e.preventDefault();
      console.info("[pwa] beforeinstallprompt fired — install button will show");
      setDeferredPrompt(e as BeforeInstallPromptEvent);
    };
    const onInstalled = () => {
      console.info("[pwa] appinstalled — app is now installed");
      setInstalled(true);
      setDeferredPrompt(null);
    };
    window.addEventListener("beforeinstallprompt", onBeforeInstall);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onBeforeInstall);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  const install = useCallback(async () => {
    if (!deferredPrompt) return;
    await deferredPrompt.prompt();
    const { outcome } = await deferredPrompt.userChoice;
    if (outcome === "accepted") {
      setInstalled(true);
    }
    // Whether accepted or dismissed, the prompt is one-shot per
    // browser session — clear it.
    setDeferredPrompt(null);
  }, [deferredPrompt]);

  return {
    // `supported` is the unified "show the install button" flag
    // for both Chromium browsers (where we captured the prompt)
    // AND iOS Safari (where we surface a separate Share hint).
    // The Layout combines this with iOS detection to decide what
    // button to render.
    supported: deferredPrompt !== null && !installed,
    installed,
    install,
  };
}
