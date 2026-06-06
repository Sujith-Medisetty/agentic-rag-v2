// React hook for the PWA install prompt — bulletproof edition.
//
// Why module-level state (NOT React state): the expert's exact concern
// applies here. If the event handler is bound via useEffect AFTER the
// event has fired, OR if the component that holds the state
// re-mounts/unmounts (HMR, route changes, etc.), the React state
// reference is lost. A module-level variable survives all of that —
// the prompt is captured at the top-level script scope and lives for
// the lifetime of the page, regardless of what React is doing.
//
// The button itself is ALWAYS visible (when not installed) so the
// user always has a way to install. The click handler tries the
// captured native prompt first, falls back to a platform-specific
// instructions modal if the prompt isn't available yet.

import { useCallback, useEffect, useState } from "react";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

// ─── MODULE-LEVEL state — survives all React re-renders ──────────────
// `savedPrompt` is captured at the very top of the script (via the
// global beforeinstallprompt listener installed in the IIFE below).
// It persists across HMR, route changes, devtools refresh, etc.
// `capturedAt` is the timestamp so we can tell in DevTools exactly
// when the browser offered install.
let savedPrompt: BeforeInstallPromptEvent | null = null;
let capturedAt: number | null = null;

// Exposed for the diagnostics modal so it can show "Prompt fired
// in this session" without needing its own listener.
(window as any).__pwa_prompt_seen = (window as any).__pwa_prompt_seen ?? false;

// IIFE: bind the listener AT MODULE LOAD TIME so we catch the event
// even if it fires before React mounts. The listener is bound once
// (no `addEventListener` accumulation across HMR), and stored on
// window so we can re-bind cleanly on HMR.
function installGlobalListener() {
  const w = window as any;
  if (w.__ojas_pwa_listener_installed) {
    // Already installed (HMR dedupe)
    return;
  }
  w.__ojas_pwa_listener_installed = true;
  window.addEventListener("beforeinstallprompt", (e: Event) => {
    // CRITICAL: preventDefault() keeps the event from expiring and
    // also stops the browser's own mini-infobar from appearing for
    // 2 seconds (which is what the user is hitting). Without this
    // call, the event is one-shot and vanishes immediately.
    e.preventDefault();
    savedPrompt = e as BeforeInstallPromptEvent;
    capturedAt = Date.now();
    w.__pwa_prompt_seen = true;
    // Also set the legacy flag the diagnostics modal reads.
    w.__ojas_pwa_prompt_captured_at = capturedAt;
    console.info(
      "[pwa] beforeinstallprompt captured at module scope — install button enabled",
    );
  });
  window.addEventListener("appinstalled", () => {
    savedPrompt = null;
    capturedAt = null;
    console.info("[pwa] appinstalled — savedPrompt cleared");
  });
}
installGlobalListener();

export function useInstallPWA() {
  const [installed, setInstalled] = useState(false);
  // The React state for the captured prompt exists ONLY so the
  // component re-renders when the global variable changes. The
  // actual prompt lives in the module-level `savedPrompt` above.
  const [, forceRerender] = useState(0);
  // Tick a timer once to detect a captured prompt that arrives
  // AFTER initial mount. Without this, if the event fires later
  // (e.g. after the user has been on the page a while), we'd
  // never re-render to pick it up. Polling is cheaper than
  // building a full event-bus here.
  useEffect(() => {
    const id = setInterval(() => forceRerender((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

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
      console.info("[pwa] Not in standalone mode — install button always visible");
    }
  }, []);

  const install = useCallback(async () => {
    if (!savedPrompt) {
      console.info("[pwa] No saved prompt — caller should fall back to manual modal");
      return;
    }
    await savedPrompt.prompt();
    const { outcome } = await savedPrompt.userChoice;
    if (outcome === "accepted") {
      setInstalled(true);
    }
    // Whether accepted or dismissed, the prompt is one-shot per
    // browser session — clear it. (Chrome won't fire
    // beforeinstallprompt again until the user re-engages.)
    savedPrompt = null;
    capturedAt = null;
    forceRerender((n) => n + 1);
  }, []);

  return {
    // The button is always visible (when not installed) because we
    // want the user to ALWAYS have a way to install. If the
    // saved prompt isn't available yet, the button click falls
    // back to the manual-instructions modal.
    supported: !installed,
    installed,
    install,
    // Expose the module-level `savedPrompt` so callers can decide
    // whether to show "Install with browser dialog" vs "Show
    // instructions".
    hasSavedPrompt: savedPrompt !== null,
  };
}
