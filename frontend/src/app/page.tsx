import Link from "next/link";
import { connection } from "next/server";

import { AutoRefresh } from "@/components/AutoRefresh";
import { BackendDown, Section, StatusBadge } from "@/components/ui";
import { getHealth, getRuns, hasActiveJob, type RunSummary } from "@/lib/api";
import { dateTime, jobLabel, usd } from "@/lib/format";

function Check({ label, ok }: { label: string; ok: boolean }) {
  return (
    <li className="flex items-center gap-1.5">
      <span className={`h-2 w-2 rounded-full ${ok ? "bg-emerald-500" : "bg-amber-500"}`} />
      <span>{label}</span>
      <span className={ok ? "text-emerald-600" : "text-amber-600"}>{ok ? "ok" : "missing"}</span>
    </li>
  );
}

function RunsTable({ runs }: { runs: RunSummary[] }) {
  if (runs.length === 0) {
    return (
      <p className="text-sm text-zinc-500">
        No runs yet. <Link href="/runs/new" className="underline">Start one</Link>.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <table className="w-full min-w-max text-sm">
        <thead className="text-left text-xs text-zinc-500">
          <tr className="border-b border-zinc-200 dark:border-zinc-800">
            <th className="px-3 py-2">Run</th>
            <th className="px-3 py-2">Market</th>
            <th className="px-3 py-2">Status</th>
            <th className="px-3 py-2">Stages</th>
            <th className="px-3 py-2">Spent / budget</th>
            <th className="px-3 py-2">Opportunities</th>
            <th className="px-3 py-2">Created</th>
            <th className="px-3 py-2">Latest job</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className="border-t border-zinc-100 hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-800/50">
              <td className="px-3 py-2 tabular-nums">
                <Link href={`/runs/${r.id}`} className="font-medium underline decoration-dotted">
                  #{r.id}
                </Link>
              </td>
              <td className="px-3 py-2">
                {r.country} · {r.industry}
                {r.problem_area ? <span className="text-zinc-500"> · {r.problem_area}</span> : null}
              </td>
              <td className="px-3 py-2">
                <StatusBadge value={r.status} />
              </td>
              <td className="px-3 py-2 tabular-nums">
                {r.stages_done}/{r.stages_total}
                {r.current_stage ? <span className="text-zinc-500"> · {r.current_stage}</span> : null}
              </td>
              <td className="px-3 py-2 tabular-nums">
                {usd(r.spent_usd)} / {usd(r.budget_usd)}
              </td>
              <td className="px-3 py-2 tabular-nums">{r.opportunity_count}</td>
              <td className="px-3 py-2 whitespace-nowrap">{dateTime(r.created_at)}</td>
              <td className="px-3 py-2">
                {r.latest_job ? (
                  <span className="inline-flex items-center gap-1.5">
                    <StatusBadge value={r.latest_job.status} />
                    <span className="text-xs text-zinc-500">{jobLabel(r.latest_job)}</span>
                  </span>
                ) : (
                  <span className="text-zinc-400">–</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function Home() {
  await connection();
  const health = await getHealth();
  const runs = health ? await getRuns().catch(() => null) : null;
  const active = (runs ?? []).some((r) => hasActiveJob(r.latest_job));

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">Research runs</h1>
          <p className="text-zinc-500">Evidence first. Ideas second.</p>
        </div>
        <AutoRefresh active={active} ms={5000} />
      </div>

      <div className="mt-4">
        {health ? (
          <ul className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
            <Check label={`API v${health.version}`} ok />
            <Check label="Database" ok={health.database} />
            <Check label="OpenAI key" ok={health.llm_key_configured} />
            <Check label="Search key" ok={health.search_key_configured} />
          </ul>
        ) : (
          <BackendDown />
        )}
      </div>

      {health && (
        <Section title="Runs">
          {runs ? <RunsTable runs={runs} /> : <p className="text-sm text-red-600">Could not load runs.</p>}
        </Section>
      )}
    </main>
  );
}
