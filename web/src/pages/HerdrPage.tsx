/**
 * HerdrPage — minimal dashboard panel for the Herdr close/reopen feature: list every
 * closed-but-reopenable agent session this install has recorded, and reopen one into a fresh
 * pane. The real logic lives server-side in hermes_cli/herdr_pane_lifecycle.py /
 * herdr_session_store.py; this page is a thin list + button, same minimal shape as the CLI's
 * `hermes herdr list` / `hermes herdr reopen`.
 */
import { useCallback, useEffect, useState } from "react";
import { RotateCw } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card } from "@nous-research/ui/ui/components/card";
import { herdrApi, type HerdrClosedSession } from "@/lib/herdr-api";
import { cn } from "@/lib/utils";

export default function HerdrPage() {
  const [records, setRecords] = useState<Record<string, HerdrClosedSession> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(() => {
    herdrApi
      .getHerdrClosedSessions()
      .then((res) => setRecords(res.records))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onReopen = useCallback(
    async (name: string, machine: string | null) => {
      setBusy(name);
      setError(null);
      try {
        await herdrApi.reopenHerdrSession(name, machine || undefined);
        refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  const entries = records ? Object.entries(records) : [];

  return (
    <div className={cn("flex min-h-0 w-full min-w-0 flex-1 flex-col gap-3 pt-1 sm:pt-2")}>
      {error && (
        <div className="rounded-sm border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {entries.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">
          No closed Herdr sessions recorded. Close one with `hermes herdr close &lt;target&gt;`.
        </p>
      )}
      {entries.map(([name, record]) => (
        <Card key={name} className="flex items-center justify-between gap-3 p-3">
          <div className="min-w-0">
            <div className="truncate font-medium">{name}</div>
            <div className="truncate text-xs text-muted-foreground">
              {record.kind} · {record.machine_id || "local"} · {record.cwd || "?"}
            </div>
          </div>
          <Button
            size="sm"
            disabled={busy === name}
            onClick={() => onReopen(name, record.machine_id)}
          >
            <RotateCw className={cn("size-3.5", busy === name && "animate-spin")} />
            Reopen
          </Button>
        </Card>
      ))}
    </div>
  );
}
