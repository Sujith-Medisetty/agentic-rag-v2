// iOS Safari can't trigger an install prompt programmatically — users have
// to tap the Share icon and pick "Add to Home Screen" themselves. This
// component shows a one-time hint banner for iOS users who haven't yet
// installed the app.
//
// Detection rules:
//   - iOS (iPhone / iPad)            → checked via UA + iPadOS-on-Mac hack
//   - Safari (not Chrome iOS / etc.) → iOS Chrome can't install PWAs at all,
//                                       so we suppress the hint there
//   - NOT already standalone         → if installed, no hint
//   - NOT dismissed in the last 14d
//
// The Apple share-icon glyph is inline SVG so we don't ship an image asset.

import { useEffect, useState } from "react";

const DISMISS_KEY = "agentic-rag.ios-install-hint.dismissed-at";
const DISMISS_TTL_MS = 14 * 24 * 60 * 60 * 1000;

function isIos(): boolean {
  const ua = window.navigator.userAgent;
  if (/iPhone|iPad|iPod/i.test(ua)) return true;
  // iPadOS 13+ reports as Macintosh; detect by Touch support.
  if (
    /Macintosh/i.test(ua) &&
    typeof window.navigator.maxTouchPoints === "number" &&
    window.navigator.maxTouchPoints > 1
  ) {
    return true;
  }
  return false;
}

function isSafari(): boolean {
  const ua = window.navigator.userAgent;
  // True Safari on iOS — exclude Chrome (CriOS), Firefox (FxiOS), Edge (EdgiOS), etc.
  return /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|YaBrowser/.test(ua);
}

function isStandalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // @ts-expect-error legacy iOS-only property
    window.navigator.standalone === true
  );
}

function isDismissedRecently(): boolean {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return false;
    return Date.now() - parseInt(raw, 10) < DISMISS_TTL_MS;
  } catch {
    return false;
  }
}

export default function IosInstallHint() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!isIos()) return;
    if (!isSafari()) return;       // iOS Chrome / Firefox can't install
    if (isStandalone()) return;
    if (isDismissedRecently()) return;
    // Slight delay so the hint doesn't fight the first paint.
    const t = setTimeout(() => setVisible(true), 1500);
    return () => clearTimeout(t);
  }, []);

  if (!visible) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(DISMISS_KEY, String(Date.now()));
    } catch {
      /* ignore */
    }
    setVisible(false);
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-30 mx-auto w-[min(480px,calc(100vw-16px))] rounded-t-lg border-x border-t border-border bg-elevated px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] shadow-lg">
      <div className="flex items-start gap-3">
        <div className="flex-1 text-sm">
          <div className="font-medium">Install agentic-rag</div>
          <div className="mt-0.5 flex items-center gap-1 text-muted">
            Tap
            <ShareIcon />
            then <span className="font-medium text-text">Add to Home Screen</span>
          </div>
        </div>
        <button
          onClick={dismiss}
          className="rounded border border-border px-2 py-1 text-xs"
        >
          Got it
        </button>
      </div>
    </div>
  );
}

function ShareIcon() {
  return (
    <svg
      viewBox="0 0 50 50"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      className="inline-block text-accent"
      aria-label="Share"
    >
      <path d="M25 4 v30 M16 13 l9 -9 l9 9" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M10 22 v22 h30 v-22" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
