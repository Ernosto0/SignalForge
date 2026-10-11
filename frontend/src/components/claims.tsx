// Claim chips: every claim id in the UI renders as a pill that expands (native <details>, no JS)
// to its statement, kind, entailment and quotes with source links. Inferences and hypotheses link
// one level down to the claims they derive from (the API includes those in the map).
import type { ClaimMap, ClaimOut } from "@/lib/api";

import { StatusBadge } from "./ui";

const KIND_STYLE: Record<string, string> = {
  fact: "border-emerald-300 text-emerald-800 dark:border-emerald-700 dark:text-emerald-300",
  inference: "border-sky-300 text-sky-800 dark:border-sky-700 dark:text-sky-300",
  hypothesis: "border-amber-400 border-dashed text-amber-800 dark:border-amber-600 dark:text-amber-300",
  assumption: "border-violet-300 border-dashed text-violet-800 dark:border-violet-700 dark:text-violet-300",
};

function Quote({ e }: { e: ClaimOut["excerpts"][number] }) {
  return (
    <blockquote className="mt-2 rounded-md bg-zinc-50 p-2 text-xs dark:bg-zinc-950">
      <p lang="tr">„{e.quote}”</p>
      {e.translation && <p className="mt-1 italic text-zinc-500">{e.translation}</p>}
      <p className="mt-1 flex flex-wrap gap-x-2 text-zinc-500">
        <a href={e.url} target="_blank" rel="noreferrer" className="underline decoration-dotted hover:text-zinc-800 dark:hover:text-zinc-200">
          {e.domain}
        </a>
        {e.source_category && <span>{e.source_category}</span>}
        {e.quality_tier && <span>{e.quality_tier} tier</span>}
        {e.verified === "fuzzy" && <span className="text-amber-600">fuzzy match</span>}
      </p>
    </blockquote>
  );
}

export function ClaimChip({ id, claims, depth = 0 }: { id: number; claims: ClaimMap; depth?: number }) {
  const claim = claims[String(id)];
  if (!claim) {
    return (
      <span className="inline-flex rounded-full border border-red-400 px-2 py-0.5 text-xs text-red-700 dark:text-red-300">
        c{id} missing
      </span>
    );
  }
  const unsourced = claim.kind === "assumption" && claim.excerpts.length === 0;
  return (
    <details className="group inline-block max-w-full align-top open:block open:my-1">
      <summary
        className={`inline-flex cursor-pointer list-none items-center gap-1 rounded-full border bg-white px-2 py-0.5 text-xs dark:bg-zinc-900 ${KIND_STYLE[claim.kind] ?? "border-zinc-300"}`}
        title={claim.statement}
      >
        c{claim.id}
        <span className="opacity-70">· {claim.kind}</span>
        {claim.entailment === "partial" && <span className="text-amber-600">· partial</span>}
      </summary>
      <div className="mt-1 rounded-md border border-zinc-200 p-2 text-sm dark:border-zinc-800">
        <div className="flex flex-wrap items-center gap-1.5">
          <StatusBadge value={claim.kind} />
          {claim.entailment && <StatusBadge value={claim.entailment} />}
          {unsourced && <span className="text-xs text-zinc-500">no quote</span>}
        </div>
        <p className="mt-1">{claim.statement}</p>
        {claim.excerpts.map((e) => (
          <Quote key={e.id} e={e} />
        ))}
        {depth === 0 && (claim.derived_from?.length ?? 0) > 0 && (
          <div className="mt-2 text-xs text-zinc-500">
            Derived from: <ClaimList ids={claim.derived_from} claims={claims} depth={1} />
          </div>
        )}
      </div>
    </details>
  );
}

export function ClaimList({
  ids,
  claims,
  depth = 0,
}: {
  ids: (number | null | undefined)[] | null | undefined;
  claims: ClaimMap;
  depth?: number;
}) {
  const unique = [...new Set((ids ?? []).filter((i): i is number => typeof i === "number"))];
  if (unique.length === 0) return <span className="text-xs text-zinc-400">no citation</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {unique.map((id) => (
        <ClaimChip key={id} id={id} claims={claims} depth={depth} />
      ))}
    </span>
  );
}
