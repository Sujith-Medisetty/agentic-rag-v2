/**
 * PWA install capture — module-scoped event listeners that survive
 * across React renders, so any component mounted anywhere in the tree
 * can ask "can I prompt the user to install the app?" and get a
 * synchronous answer.
 *
 * The browser fires `beforeinstallprompt` AT MOST ONCE per page load,
 * and only when the PWA install criteria are met. If we don't catch
 * it inside a module-scope listener, it's lost — React's lifecycle
 * means components might not be mounted yet when it fires.
 *
 * `beforeinstallprompt` event shape is not yet in the standard
 * TypeScript lib, so we declare the extra fields here.
 */

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

let deferred: BeforeInstallPromptEvent | null = null;
const listeners = new Set<() => void>();

if (typeof window !== "undefined") {
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e as BeforeInstallPromptEvent;
    listeners.forEach((l) => l());
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    listeners.forEach((l) => l());
  });
}

function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // iOS Safari exposes this when launched from the home screen
    (window.navigator as unknown as { standalone?: boolean }).standalone ===
      true
  );
}

function isIOS(): boolean {
  if (typeof navigator === "undefined") return false;
  return (
    /iPad|iPhone|iPod/.test(navigator.userAgent) &&
    !/MSStream/.test(navigator.userAgent)
  );
}

/** Subscribe to install-state changes. Returns an unsubscribe fn. */
function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function getDeferred(): BeforeInstallPromptEvent | null {
  return deferred;
}

function clearDeferred(): void {
  deferred = null;
  listeners.forEach((l) => l());
}

export const pwaInstall = {
  subscribe,
  getDeferred,
  clearDeferred,
  isStandalone,
  isIOS,
};
