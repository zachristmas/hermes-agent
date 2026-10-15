/**
 * Dashboard client for the minimal Herdr close/reopen panel (HerdrPage.tsx). Split out of
 * lib/api.ts as its own sibling rather than growing that file further: api.ts is already over
 * this repo's file-size health cap, so new behaviour goes in a fresh file instead (same remedy
 * `scripts/code_health` prescribes on the Python side).
 */
import { fetchJSON } from "@/lib/api";

/** One closed-but-reopenable Herdr agent session (hermes_cli/herdr_session_store.py). */
export interface HerdrClosedSession {
  name: string;
  kind: string;
  session_id: string;
  cwd: string | null;
  machine_id: string | null;
  closed_pane_id: string | null;
}

export interface HerdrClosedSessionsResponse {
  records: Record<string, HerdrClosedSession>;
}

export interface HerdrReopenResponse {
  name: string;
  pane_id: string;
  agent_status: string;
}

export const herdrApi = {
  getHerdrClosedSessions: () =>
    fetchJSON<HerdrClosedSessionsResponse>("/api/herdr/closed"),
  reopenHerdrSession: (target: string, machine?: string) =>
    fetchJSON<HerdrReopenResponse>("/api/herdr/reopen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, machine: machine || undefined }),
    }),
};
