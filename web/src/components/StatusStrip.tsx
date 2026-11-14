/** Live dependency health: polls the PUBLIC /readyz every 15s and renders one
 * dot per check (redis/db/s3). Zero auth — it's the ops surface of the API. */

import { useEffect, useState } from "react";
import type { ReadyBody } from "../types";

const CHECKS = ["redis", "db", "s3"] as const;
const POLL_MS = 15000;

export function StatusStrip(): React.JSX.Element {
  const [body, setBody] = useState<ReadyBody | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let alive = true;
    async function poll(): Promise<void> {
      try {
        const resp = await fetch("http://localhost:8000/readyz");
        const data = (await resp.json()) as ReadyBody;
        if (alive) {
          setBody(data);
          setUnreachable(false);
        }
      } catch {
        if (alive) setUnreachable(true);
      }
    }
    void poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const allUp =
    body?.status === "ready" && CHECKS.every((c) => body.checks[c] === "up");

  return (
    <div className={`status-strip ${unreachable || (body && !allUp) ? "bad" : "ok"}`}>
      <span className="strip-label">stack</span>
      {unreachable ? (
        <span className="dot bad" title="API unreachable" />
      ) : (
        CHECKS.map((c) => {
          const state = body?.checks[c] ?? "…";
          return (
            <span
              key={c}
              className={`dot ${state === "up" ? "ok" : state === "down" ? "bad" : "unknown"}`}
              title={`${c}: ${state}`}
            />
          );
        })
      )}
      <span className="strip-text">
        {unreachable ? "API unreachable" : body ? (allUp ? "all up" : "degraded") : "checking…"}
      </span>
    </div>
  );
}
