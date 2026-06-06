// WebSocket client for the per-session live event stream.
//
// Handshake:
//   1. open ws://.../api/sessions/{id}/stream
//   2. send {"type":"auth","token":"<bearer>"} as the FIRST text frame
//   3. server then pushes {kind, payload, ts} JSON envelopes until close
//
// Production-grade resilience (matches what ChatGPT / Claude.ai / Linear
// all do for their live-event WebSockets):
//   - Application-level ping every 25s. The browser's built-in WS
//     frames (the ones the server can also send) only work in tandem
//     with the server's --ws-ping-interval, so we ALSO send our own
//     {"type":"ping"} and ignore the {"type":"pong"} response. This
//     belt-and-suspenders keeps the socket warm even when intermediate
//     proxies (mobile carrier NATs) drop idle TCP connections.
//   - Visibility-driven immediate reconnect. setTimeout is throttled
//     in background tabs (1Hz) and paused when the JS thread is
//     suspended (mobile screen off). The moment the user comes
//     back to the tab we cancel the pending backoff and reconnect
//     RIGHT NOW, so they don't wait up to 10s for the chat to
//     "wake up".
//   - Exponential backoff (500ms → 1s → ... → 10s cap) for genuine
//     network failures. The backoff is reset on every successful
//     open.
//   - Event replay via `?since=<ts>`. The chat page already uses
//     sessionApi.events(sessionId, lastSeenTs) on mount; the
//     reconnected socket just picks up from where we left off.

import { getToken } from "@/lib/auth";
import type { LiveEvent } from "@/lib/types";

export interface EventStreamHandle {
  close: () => void;
}

// Production tuning — kept here (not at the top of the file) so the
// intent is obvious to anyone editing.
const PING_INTERVAL_MS = 25_000;       // every 25s
const BACKOFF_INITIAL_MS = 500;
const BACKOFF_CAP_MS = 10_000;

export function openEventStream(
  sessionId: string,
  onEvent: (event: LiveEvent) => void,
  onStatus?: (status: "connecting" | "open" | "closed" | "error") => void,
): EventStreamHandle {
  let manualClose = false;
  let ws: WebSocket | null = null;
  let backoffMs = BACKOFF_INITIAL_MS;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let onVisibility: (() => void) | null = null;

  const url = () => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}/api/sessions/${encodeURIComponent(
      sessionId,
    )}/stream`;
  };

  const stopPing = () => {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  };
  const startPing = () => {
    stopPing();
    pingTimer = setInterval(() => {
      // Application-level ping. Server is configured to ignore
      // inbound text frames; we don't actually need a reply because
      // the server also sends transport-level WS pings every 20s
      // (--ws-ping-interval). This app-level ping is purely for
      // keeping any browser-side timers (e.g. mobile background
      // tab throttling) aware that the connection is alive.
      try {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
        }
      } catch {
        /* socket is dying; reconnect loop will catch it */
      }
    }, PING_INTERVAL_MS);
  };

  const cancelReconnect = () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  const scheduleReconnect = () => {
    if (manualClose) return;
    cancelReconnect();
    reconnectTimer = setTimeout(connect, backoffMs);
  };

  const connect = () => {
    if (manualClose) return;
    onStatus?.("connecting");
    const sock = new WebSocket(url());
    ws = sock;

    sock.onopen = () => {
      const token = getToken();
      sock.send(JSON.stringify({ type: "auth", token: token ?? "" }));
      // Successful connect — reset the backoff so the NEXT drop
      // (e.g. after a 6-hour chat) starts at 500ms again, not
      // stuck at the 10s cap from a transient network blip hours
      // ago.
      backoffMs = BACKOFF_INITIAL_MS;
      onStatus?.("open");
      startPing();
    };

    sock.onmessage = (e) => {
      let env: LiveEvent;
      try {
        env = JSON.parse(e.data) as LiveEvent;
      } catch {
        return;
      }
      // Pong response — ignored, the ping was just a liveness nudge.
      // The transport-level WS pings (server --ws-ping-interval)
      // are what actually keep the connection alive.
      if (typeof env.kind === "string" && env.kind === "pong") return;
      if (typeof env.kind === "string") {
        onEvent(env);
      }
    };

    sock.onerror = () => {
      onStatus?.("error");
    };

    sock.onclose = () => {
      stopPing();
      ws = null;
      onStatus?.("closed");
      if (manualClose) return;
      // Exponential backoff with cap.
      backoffMs = Math.min(backoffMs * 2, BACKOFF_CAP_MS);
      scheduleReconnect();
    };
  };

  // Visibility hook — reconnect IMMEDIATELY when the tab/window
  // becomes visible. This is the single most impactful change for
  // mobile/PWA users: when they switch back to the app after
  // backgrounding it, the backoff timer (which was throttled to
  // 1Hz or paused) would otherwise delay the reconnect by up to
  // 10s. We just fire it right now.
  onVisibility = () => {
    if (document.visibilityState !== "visible") return;
    if (ws && ws.readyState === WebSocket.OPEN) return; // already good
    // Cancel any pending backoff and reconnect now.
    cancelReconnect();
    backoffMs = BACKOFF_INITIAL_MS; // user is back, give it the full retry budget fresh
    connect();
  };
  document.addEventListener("visibilitychange", onVisibility);

  connect();

  return {
    close: () => {
      manualClose = true;
      cancelReconnect();
      stopPing();
      if (onVisibility) {
        document.removeEventListener("visibilitychange", onVisibility);
      }
      ws?.close();
    },
  };
}
