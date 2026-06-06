// PWA install prompt — singleton that captures the browser's
// `BeforeInstallPromptEvent` BEFORE React mounts, so even on the very
// first paint we already know whether install is available. Components
// subscribe via `subscribe()` to re-render when the state flips.
//
// Why not just use a React state? Because the `beforeinstallprompt` event
// can fire before any component mounts — if we wait for React, we miss it
// entirely on returning visits. Capturing at module-load time fixes that.

// The captured event from `beforeinstallprompt`. The shape isn't in the
// stock TypeScript DOM lib, so we type the bits we touch ourselves.
type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
};

let deferred: BeforeInstallPromptEvent | null = null;
let installed = false;
const listeners = new Set<() => void>();

function notify() {
  for (const fn of listeners) fn();
}

if (typeof window !== "undefined") {
  // Browser fires this when its install heuristic is satisfied AND the user
  // isn't already installed. We grab the event and prevent the mini-infobar
  // so we can render OUR own install button instead.
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e as BeforeInstallPromptEvent;
    notify();
  });

  // Fires after a successful install (user accepted our or browser's prompt).
  // We use this to hide the install button immediately so nothing nags an
  // already-installed user.
  window.addEventListener("appinstalled", () => {
    installed = true;
    deferred = null;
    notify();
  });
}

/** True if the page is being rendered inside an installed PWA (standalone
 *  mode). On iOS we also check `navigator.standalone` since iOS Safari
 *  doesn't set `display-mode: standalone` on older versions. */
export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  if (window.matchMedia("(display-mode: standalone)").matches) return true;
  const nav = window.navigator as unknown as { standalone?: boolean };
  return !!nav.standalone;
}

/** True on iOS Safari (no install API). We render an instruction sheet
 *  instead of a real prompt, because Apple doesn't expose one. */
export function isIOS(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iPhone|iPad|iPod/i.test(navigator.userAgent)
    && !(window as unknown as { MSStream?: unknown }).MSStream;
}

/** Has the browser already given us a deferred install event? */
export function canPromptInstall(): boolean {
  return !installed && !isStandalone() && deferred !== null;
}

/** True if we should still show SOMETHING (button or iOS hint) — i.e.
 *  the app isn't installed AND we're not running standalone. The button's
 *  behaviour differs (real prompt vs iOS guide) based on platform. */
export function shouldOfferInstall(): boolean {
  return !installed && !isStandalone();
}

/** Re-render hook. Components call this in `useEffect` to subscribe to
 *  install-state changes; returns an unsubscribe function. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** Trigger the captured native prompt. Resolves to the user's choice or
 *  `null` if there was no captured event. After the prompt resolves the
 *  event is consumed (can't be re-prompted) — listeners are notified so
 *  the button can hide itself. */
export async function triggerInstall(): Promise<"accepted" | "dismissed" | null> {
  if (!deferred) return null;
  try {
    await deferred.prompt();
    const choice = await deferred.userChoice;
    deferred = null;
    notify();
    return choice.outcome;
  } catch {
    // Some browsers throw if `prompt()` is called outside a user gesture.
    deferred = null;
    notify();
    return null;
  }
}
