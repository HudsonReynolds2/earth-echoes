/**
 * The live-update socket (task E3.12; spec 13).
 *
 * **Events are invalidation signals, never data.** The socket carries what
 * changed, and this hook responds by asking react-query to refetch — it never
 * patches a cache from an event body. Two reasons, and both are about being
 * honest rather than clever: Postgres `NOTIFY` is best-effort, so a browser
 * that reconnects has missed whatever happened while it was away and a
 * patched cache would be silently stale; and the server already applies
 * scoping and derivation that an event payload does not repeat, so a refetch
 * is the only way the screen agrees with the API.
 *
 * That also makes the reconnect story simple: on open, invalidate everything
 * this socket feeds. Whatever was missed is picked up in one round trip.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { apiBaseUrl } from "./api";

export type LiveChannel = "device_status" | "reconciliation";

export interface LiveEvent {
  channel: LiveChannel;
  deployment_id: string;
  entity_type: string;
  entity_id: string;
  data: Record<string, unknown>;
  at: string;
}

/** RFC 6455 policy violation: the server said "not you", not "try again". */
const CLOSE_UNAUTHORIZED = 1008;

function socketUrl(): string {
  const base = apiBaseUrl();
  return `${base.replace(/^http/, "ws")}/api/v1/ws`;
}

/**
 * Hold one socket for the app's lifetime and refetch on what it reports.
 *
 * Mounted once, in the shell. One socket per tab rather than one per
 * component: every panel wants the same events, and N sockets would multiply
 * the server's fan-out by the number of open cards for no added information.
 */
export function useLiveUpdates(enabled: boolean): void {
  const queryClient = useQueryClient();
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<number | null>(null);
  const attemptsRef = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    let closed = false;

    const invalidate = (event: LiveEvent) => {
      // Coarse on purpose. A device status change moves the inventory tables,
      // the pod card and the overview roll-up; naming each key here would
      // couple this hook to every screen's query shape, and the refetches are
      // cheap next to the round trip that delivered the event.
      if (event.channel === "device_status") {
        void queryClient.invalidateQueries({ queryKey: ["aggregators"] });
        void queryClient.invalidateQueries({ queryKey: ["listeners"] });
        void queryClient.invalidateQueries({ queryKey: ["pods"] });
        void queryClient.invalidateQueries({ queryKey: ["deployments"] });
        void queryClient.invalidateQueries({ queryKey: ["hierarchy"] });
      }
      if (event.channel === "reconciliation") {
        void queryClient.invalidateQueries({ queryKey: ["timeline"] });
        void queryClient.invalidateQueries({ queryKey: ["revisions"] });
        void queryClient.invalidateQueries({ queryKey: ["aggregators"] });
        void queryClient.invalidateQueries({ queryKey: ["listeners"] });
      }
    };

    const connect = () => {
      if (closed) return;
      let socket: WebSocket;
      try {
        socket = new WebSocket(socketUrl());
      } catch {
        // A malformed base URL, or an environment with no WebSocket. Live
        // updates are an enhancement; the app still works by refetching.
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        attemptsRef.current = 0;
        // Everything is refetched on (re)connect: see the module docstring on
        // why a missed event must not leave a stale screen.
        void queryClient.invalidateQueries();
      };
      socket.onmessage = (message) => {
        try {
          invalidate(JSON.parse(String(message.data)) as LiveEvent);
        } catch {
          // A payload this build does not understand is not worth breaking
          // the socket over; the next real event still lands.
        }
      };
      socket.onclose = (event) => {
        socketRef.current = null;
        if (closed || event.code === CLOSE_UNAUTHORIZED) {
          // Not authenticated, or deliberately torn down. Retrying an
          // unauthorized socket would hammer the API with a login it does not
          // have; the next sign-in remounts this hook.
          return;
        }
        attemptsRef.current += 1;
        const delay = Math.min(1000 * 2 ** (attemptsRef.current - 1), 30_000);
        retryRef.current = window.setTimeout(connect, delay);
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      closed = true;
      if (retryRef.current !== null) window.clearTimeout(retryRef.current);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [enabled, queryClient]);
}
