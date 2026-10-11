import Link from "next/link";
import { notFound } from "next/navigation";

import { AutoRefresh } from "@/components/AutoRefresh";
import { OppCard } from "@/components/OppCard";
import { RunActions } from "@/components/RunActions";
import { BackendDown, Card, JsonBlock, Section, Stat, StatusBadge } from "@/components/ui";
import {
  BackendUnavailable,
  getRun,
  getRunOpportunities,
  hasActiveJob,
  reportUrl,
  type AgentProgress,
} from "@/lib/api";
import { dateTime, duration, jobLabel, usd } from "@/lib/format";

function AgentRow({ agent }: { agent: AgentProgress }) {
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium">{agent.name}</span>
        <StatusBadge value={agent.status} />
        <span className="text-xs text-zinc-500">{agent.description}</span>
        <span className="ml-auto text-sm tabular-nums text-zinc-500">
          {duration(agent.started_at, agent.finished_at)} · {usd(agent.cost_usd, 4)}
        </span>
      </div>
      <ul className="mt-2 space-y-1 border-l border-zinc-200 pl-3 dark:border-zinc-800">
        {agent.stages.map((s) => (
          <li key={s.name} className="text-sm">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-mono text-xs">{s.name}</span>
              <StatusBadge value={s.status} />
              <span className="ml-auto tabular-nums text-xs text-zinc-500">
                {s.status === "pending" ? "–" : duration(s.started_at, s.finished_at)} · {usd(s.cost_usd, 4)}
              </span>
            </div>
            {s.error && <p className="mt-1 break-words text-xs text-red-600 dark:text-red-400">{s.error}</p>}
            {s.metrics && Object.keys(s.metrics).length > 0 && (
              <div className="mt-1">
                <JsonBlock value={s.metrics} summary="metrics" />
              </div>
            )}
          </li>
        ))}
      </ul>
    </li>
  );
}

export default async function RunPage(props: PageProps<"/runs/[id]">) {
  const { id } = await props.params;
  const runId = Number(id);
  if (!Number.isInteger(runId)) notFound();

  let run, opportunities;
  try {
    [run, opportunities] = await Promise.all([getRun(runId), getRunOpportunities(runId)]);
  } catch (err) {
    if (err instanceof BackendUnavailable) {
      return (
        <main className="mx-auto w-full max-w-6xl px-4 py-8">
          <BackendDown />
        </main>
      );
    }
    throw err;
  }
  if (!run) notFound();

  const job = run.latest_job ?? run.jobs[0];
  const active = hasActiveJob(job);
  const request = run.plan?.request;

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-sm text-zinc-500">
            <Link href="/" className="hover:underline">
              Runs
            </Link>{" "}
            / #{run.id}
          </p>
          <h1 className="mt-1 text-xl font-semibold">
            {run.country} · {run.industry}
            {run.problem_area ? ` · ${run.problem_area}` : ""}
          </h1>
          {request && (
            <p className="text-sm text-zinc-500">
              {request.target_customer} · {request.business_model}
            </p>
          )}
        </div>
        <RunActions runId={run.id} agents={run.agents.map((a) => a.name)} latestJob={job} />
      </div>

      <Card className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-5">
        <Stat label="Status">
          <StatusBadge value={run.status} />
        </Stat>
        <Stat label="Spent / budget">
          {usd(run.spent_usd)} / {usd(run.budget_usd)}
        </Stat>
        <Stat label="Stages">
          {run.stages_done}/{run.stages_total}
          {run.current_stage ? ` · ${run.current_stage}` : ""}
        </Stat>
        <Stat label="Latest job">
          {job ? (
            <span className="inline-flex items-center gap-1.5">
              <StatusBadge value={job.status} />
              <span className="truncate text-xs text-zinc-500">{jobLabel(job)}</span>
            </span>
          ) : (
            "–"
          )}
        </Stat>
        <Stat label="Pack">
          {run.pack_id}@{run.pack_version}
        </Stat>
      </Card>
      {job?.error && <p className="mt-2 break-words text-sm text-red-600 dark:text-red-400">{job.error}</p>}

      <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
        <Link href={`/runs/${run.id}/sources`} className="underline decoration-dotted">
          Sources
        </Link>
        {run.report_formats.length > 0 ? (
          <span className="flex gap-2">
            Report:
            {run.report_formats.map((fmt) => (
              <a key={fmt} href={reportUrl(run.id, fmt)} target="_blank" rel="noreferrer" className="underline decoration-dotted">
                {fmt}
              </a>
            ))}
          </span>
        ) : (
          <span className="text-zinc-500">No report yet</span>
        )}
        <span className="ml-auto">
          <AutoRefresh active={active} ms={3000} />
        </span>
      </div>

      <Section title={`Opportunities (${opportunities?.length ?? 0})`}>
        {opportunities && opportunities.length > 0 ? (
          <div className="grid gap-4 md:grid-cols-2">
            {opportunities.map((o) => (
              <OppCard key={o.id} o={o} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-zinc-500">No opportunities yet.</p>
        )}
      </Section>

      <Section title="Progress">
        <Card className="py-0">
          <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {run.agents.map((a) => (
              <AgentRow key={a.name} agent={a} />
            ))}
          </ul>
        </Card>
      </Section>

      {run.jobs.length > 0 && (
        <Section title="Jobs">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-zinc-500">
                <tr>
                  <th className="py-1 pr-3">#</th>
                  <th className="py-1 pr-3">Status</th>
                  <th className="py-1 pr-3">What</th>
                  <th className="py-1 pr-3">Created</th>
                  <th className="py-1 pr-3">Took</th>
                </tr>
              </thead>
              <tbody>
                {run.jobs.map((j) => (
                  <tr key={j.id} className="border-t border-zinc-200 dark:border-zinc-800">
                    <td className="py-1 pr-3 tabular-nums">{j.id}</td>
                    <td className="py-1 pr-3">
                      <StatusBadge value={j.status} />
                    </td>
                    <td className="py-1 pr-3">{jobLabel(j)}</td>
                    <td className="py-1 pr-3 whitespace-nowrap">{dateTime(j.created_at)}</td>
                    <td className="py-1 pr-3">{duration(j.started_at, j.finished_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </main>
  );
}
