import Link from "next/link";

import type { OpportunityCard } from "@/lib/api";
import { label, list, range } from "@/lib/format";

import { Card, Stat, StatusBadge } from "./ui";

export function OppCard({ o }: { o: OpportunityCard }) {
  const knockouts = list(o.knockouts);
  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge value={o.category} />
        {o.gate2_status === "knocked_out" && <StatusBadge value="knocked_out" />}
        <span className="ml-auto text-xs text-zinc-500">#{o.id}</span>
      </div>
      <div>
        <Link href={`/opportunities/${o.id}`} className="font-medium hover:underline">
          {o.segment}
        </Link>
        <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{o.solution_angle}</p>
        <p className="mt-1 text-xs text-zinc-500">Problem: {o.problem_name}</p>
      </div>
      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Stat label="Attractiveness">{o.attractiveness ?? "–"}</Stat>
        <Stat label="Confidence">{o.confidence ?? "–"}</Stat>
        <Stat label="Founder fit">{o.founder_fit ? label(o.founder_fit) : "–"}</Stat>
        <Stat label="Value / month">{range(o.value_usd_month, "$")}</Stat>
      </div>
      {knockouts.length > 0 && (
        <ul className="text-sm text-red-700 dark:text-red-300">
          {knockouts.map((k) => (
            <li key={k.rule}>
              Knocked out: {label(k.rule)}
              {k.detail ? ` — ${k.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
