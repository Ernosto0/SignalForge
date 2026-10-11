"use client";

// Run header actions: Resume, Re-run one agent, Cancel the queued job. Each POSTs through the
// /api proxy, then refreshes the server page; 409 / 422 messages are shown inline.
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, cancelJob, enqueueJob, type Job, type JobArgs } from "@/lib/api";

const button =
  "rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800";

function message(err: unknown): string {
  if (err instanceof ApiError) {
    if (Array.isArray(err.detail)) return err.detail.map((d) => d.msg ?? String(d)).join("; ");
    return `${err.status}: ${err.message}`;
  }
  return err instanceof Error ? err.message : String(err);
}

export function RunActions({
  runId,
  agents,
  latestJob,
}: {
  runId: number;
  agents: string[];
  latestJob?: Job | null;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agent, setAgent] = useState(agents.at(-1) ?? "");
  const active = latestJob?.status === "queued" || latestJob?.status === "running";

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      router.refresh();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  const enqueue = (args: JobArgs) => act(() => enqueueJob(runId, args));

  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <div className="flex flex-wrap items-center gap-2">
        <button className={button} disabled={busy || active} onClick={() => enqueue({ resume: true })}>
          Resume
        </button>
        <span className="inline-flex">
          <select
            aria-label="Agent to re-run"
            className="rounded-l-md border border-r-0 border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            disabled={busy || active}
          >
            {agents.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <button
            className={`${button} rounded-l-none`}
            disabled={busy || active || !agent}
            onClick={() => enqueue({ agent })}
          >
            Re-run agent
          </button>
        </span>
        {latestJob?.status === "queued" && (
          <button className={button} disabled={busy} onClick={() => act(() => cancelJob(latestJob.id))}>
            Cancel queued
          </button>
        )}
      </div>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}
