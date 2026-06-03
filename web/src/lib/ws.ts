// WebSocket client for the per-session live event stream.
//
// Handshake:
//   1. open ws://.../api/sessions/{id}/stream
//   2. send {"type":"auth","token":"<bearer>"} as the FIRST text frame
//   3. server then pushes {kind, payload, ts} JSON envelopes until close
//
// On disconnect (network drop, server restart) we auto-reconnect with
// exponential backoff capped at 10s. The caller's `onEvent` is invoked
// for each well-formed envelope; malformed frames are dropped.

import { getToken } from "@/lib/auth";
import type { LiveEvent } from "@/lib/types";

export interface EventStreamHandle {
  close: () => void;
}

export function openEventStream(
  sessionId: string,
  onEvent: (event: LiveEvent) => void,
  onStatus?: (status: "connecting" | "open" | "closed" | "error") => void,
): EventStreamHandle {
  let manualClose = false;
  let ws: WebSocket | null = null;
  let backoffMs = 500;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  const url = () => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    // Same-origin path — Vite dev server proxies this to FastAPI.
    return `${proto}://${window.location.host}/api/sessions/${encodeURIComponent(
      sessionId,
    )}/stream`;
  };

  const connect = () => {
    if (manualClose) return;
    onStatus?.("connecting");
    const sock = new WebSocket(url());
    ws = sock;

    sock.onopen = () => {
      const token = getToken();
      sock.send(JSON.stringify({ type: "auth", token: token ?? "" }));
      backoffMs = 500;
      onStatus?.("open");
    };

    sock.onmessage = (e) => {
      let env: LiveEvent;
      try {
        env = JSON.parse(e.data) as LiveEvent;
      } catch {
        return;
      }
      if (typeof env.kind === "string") {
        onEvent(env);
      }
    };

    sock.onerror = () => {
      onStatus?.("error");
    };

    sock.onclose = () => {
      onStatus?.("closed");
      ws = null;
      if (manualClose) return;
      backoffMs = Math.min(backoffMs * 2, 10_000);
      reconnectTimer = setTimeout(connect, backoffMs);
    };
  };

  connect();

  return {
    close: () => {
      manualClose = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
    },
  };
}
