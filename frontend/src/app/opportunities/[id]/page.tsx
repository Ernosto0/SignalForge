import Link from "next/link";
import { notFound } from "next/navigation";

import { ClaimChip, ClaimList } from "@/components/claims";
import { BackendDown, Card, JsonBlock, Section, Stat, StatusBadge } from "@/components/ui";
import {
  BackendUnavailable,
  getOpportunity,
  type ClaimMap,
  type EconomicModel,
  type GapMatrix,
  type OpportunityDetail,
  type RoleClaim,
  type TraceEntry,
} from "@/lib/api";
import { label, list, pairOrNull, range, record, usd } from "@/lib/format";

const ROLES = ["user", "buyer", "decision_maker", "economic_beneficiary", "budget_owner"] as const;
const CELL_TONE: Record<string, string> = {
  yes: "text-emerald-700 dark:text-emerald-300",
  partial: "text-amber-700 dark:text-amber-300",
  no: "text-red-700 dark:text-red-300",
  unknown: "text-zinc-400",
};

function Factors({ o, claims }: { o: OpportunityDetail; claims: ClaimMap }) {
  const factors = Object.entries(record(o.score?.factors));
  if (factors.length === 0) return <p className="text-sm text-zinc-500">No factors (knocked out or not scored).</p>;
  return (
    <div className="space-y-3">
      {factors.map(([name, f]) => (
        <Card key={name}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{label(name)}</span>
            <span className="rounded bg-zinc-100 px-1.5 text-sm font-semibold tabular-nums dark:bg-zinc-800">
              {f.level ?? "–"}/5
            </span>
            {f.weight !== undefined && <span className="text-xs text-zinc-500">weight {f.weight}</span>}
            {f.source && <span className="text-xs text-zinc-500">{f.source === "code" ? "set by code" : "model judged"}</span>}
            {f.capped && <StatusBadge value="capped" />}
            {Array.isArray(f.judges) && f.judges.length > 1 && (
              <span className="text-xs text-zinc-500">judges {f.judges.join(", ")}</span>
            )}
          </div>
          {f.justification && <p className="mt-1 text-sm text-zinc-700 dark:text-zinc-300">{f.justification}</p>}
          <div className="mt-2">
            <ClaimList ids={list(f.claim_ids)} claims={claims} />
          </div>
        </Card>
      ))}
    </div>
  );
}

function Trace({ trace }: { trace: TraceEntry[] }) {
  if (trace.length === 0) return <p className="text-sm text-zinc-500">No rule trace.</p>;
  return (
    <ul className="divide-y divide-zinc-200 text-sm dark:divide-zinc-800">
      {trace.map((e, i) => (
        <li key={`${e.rule}-${i}`} className="py-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs">{e.rule ?? "?"}</span>
            {e.fired !== undefined && <StatusBadge value={e.fired ? "fired" : "not fired"} />}
            {e.result !== undefined && <span className="text-xs">→ {String(e.result)}</span>}
            {e.factor && <span className="text-xs text-zinc-500">{label(e.factor)}</span>}
          </div>
          {(e.inputs || e.thresholds || e.reasons) !== undefined && (
            <div className="mt-1">
              <JsonBlock value={{ inputs: e.inputs, thresholds: e.thresholds, reasons: e.reasons }} summary="inputs" />
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function Economics({ m, claims }: { m: EconomicModel; claims: ClaimMap }) {
  if (!m || Object.keys(m).length === 0) return <p className="text-sm text-zinc-500">No economic model.</p>;
  const anchor = m.competitor_anchor_usd_month;
  return (
    <Card>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Status">
          <StatusBadge value={m.status} />
        </Stat>
        <Stat label="Formula">{m.formula ? label(m.formula) : "–"}</Stat>
        <Stat label="Value / month (USD)">{range(pairOrNull(m.value_usd_month), "$")}</Stat>
        <Stat label="Price ceiling (USD)">{range(pairOrNull(m.price_ceiling_usd_month), "$")}</Stat>
        <Stat label={`Value / month (${m.currency ?? "local"})`}>{range(pairOrNull(m.value_local))}</Stat>
        <Stat label="Capture share">
          {pairOrNull(m.capture_share) ? range(pairOrNull(m.capture_share)!.map((v) => v * 100) as [number, number]) + "%" : "–"}
        </Stat>
        <Stat label="Competitor anchor">
          {anchor ? `${usd(anchor.median, 0)} median (n ${anchor.n ?? "–"})` : "–"}
        </Stat>
        <Stat label="FX">
          {m.fx?.rate ? (
            <span title={m.fx.source_url}>
              {m.fx.rate} ({m.fx.as_of ?? "undated"})
            </span>
          ) : (
            "–"
          )}
        </Stat>
      </div>
      {Object.keys(record(m.inputs)).length > 0 && (
        <div className="mt-4">
          <div className="mb-1 text-xs text-zinc-500">Assumptions</div>
          <ul className="space-y-1 text-sm">
            {Object.entries(record(m.inputs)).map(([name, claimId]) => (
              <li key={name} className="flex flex-wrap items-start gap-2">
                <span className="font-mono text-xs">{name}</span>
                <ClaimChip id={claimId} claims={claims} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {anchor?.claim_ids && anchor.claim_ids.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-xs text-zinc-500">Price anchor claims</div>
          <ClaimList ids={anchor.claim_ids} claims={claims} />
        </div>
      )}
    </Card>
  );
}

function Role({ name, role, claims }: { name: string; role: RoleClaim | null | undefined; claims: ClaimMap }) {
  return (
    <li className="py-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="w-40 shrink-0 text-zinc-500">{label(name)}</span>
        <span className="font-medium" lang="tr">
          {role?.role ?? "none"}
        </span>
        {role?.entailment && <StatusBadge value={role.entailment} />}
      </div>
      {role && (
        <div className="mt-1 flex flex-wrap items-center gap-2 sm:pl-42">
          {list(role.claim_ids).length > 0 && <ClaimList ids={role.claim_ids} claims={claims} />}
          {role.hypothesis_claim_id ? (
            <ClaimChip id={role.hypothesis_claim_id} claims={claims} />
          ) : (
            role.hypothesis && <span className="text-xs italic text-amber-700 dark:text-amber-300">Hypothesis: {role.hypothesis}</span>
          )}
        </div>
      )}
    </li>
  );
}

function Gaps({ matrix, claims }: { matrix: GapMatrix; claims: ClaimMap }) {
  const competitors = list(matrix.competitors);
  const cells = record(matrix.cells);
  return (
    <>
      <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
        <table className="w-full min-w-max text-sm">
          <thead className="bg-zinc-50 text-left text-xs text-zinc-500 dark:bg-zinc-900">
            <tr>
              <th className="px-3 py-2">Dimension</th>
              {competitors.map((c) => (
                <th key={c.id} className="px-3 py-2">
                  {c.url ? (
                    <a href={c.url} target="_blank" rel="noreferrer" className="underline decoration-dotted">
                      {c.name}
                    </a>
                  ) : (
                    c.name
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list(matrix.dimensions).map((d) => (
              <tr key={d.key} className="border-t border-zinc-200 align-top dark:border-zinc-800">
                <td className="px-3 py-2">{d.label ?? label(d.key)}</td>
                {competitors.map((c) => {
                  const cell = record(cells[d.key])[String(c.id)];
                  return (
                    <td key={c.id} className="px-3 py-2">
                      <span className={CELL_TONE[cell?.value ?? "unknown"] ?? ""}>{cell?.value ?? "unknown"}</span>
                      {cell?.claim_id ? (
                        <div className="mt-1">
                          <ClaimChip id={cell.claim_id} claims={claims} />
                        </div>
                      ) : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {list(matrix.gaps).length > 0 && (
        <ul className="mt-3 space-y-1 text-sm">
          {list(matrix.gaps).map((g) => (
            <li key={g.dimension} className="flex flex-wrap items-center gap-2">
              Gap: <span className="font-medium">{label(g.dimension)}</span>
              {g.claim_id ? <ClaimChip id={g.claim_id} claims={claims} /> : null}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

export default async function OpportunityPage(props: PageProps<"/opportunities/[id]">) {
  const { id } = await props.params;
  const oppId = Number(id);
  if (!Number.isInteger(oppId)) notFound();
  let o: OpportunityDetail | null;
  try {
    o = await getOpportunity(oppId);
  } catch (err) {
    if (err instanceof BackendUnavailable) {
      return (
        <main className="mx-auto w-full max-w-5xl px-4 py-8">
          <BackendDown />
        </main>
      );
    }
    throw err;
  }
  if (!o) notFound();

  const claims = record(o.claims);
  const roles = o.buyer_roles ?? {};
  const channels = list(o.accessibility?.channels);
  const experiment = o.score?.experiment;
  const problem = o.problem;

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8">
      <p className="text-sm text-zinc-500">
        <Link href={`/runs/${o.run_id}`} className="hover:underline">
          Run #{o.run_id}
        </Link>{" "}
        / opportunity #{o.id}
      </p>
      <h1 className="mt-1 text-xl font-semibold">{o.segment}</h1>
      <p className="mt-1 text-zinc-600 dark:text-zinc-400">{o.solution_angle}</p>

      <Card className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-5">
        <Stat label="Category">
          <StatusBadge value={o.category} />
        </Stat>
        <Stat label="Attractiveness">{o.attractiveness ?? "–"}</Stat>
        <Stat label="Confidence">
          <StatusBadge value={o.confidence} />
        </Stat>
        <Stat label="Founder fit">
          <StatusBadge value={o.founder_fit} />
        </Stat>
        <Stat label="Value / month">{range(o.value_usd_month, "$")}</Stat>
      </Card>
      {list(o.knockouts).length > 0 && (
        <ul className="mt-3 text-sm text-red-700 dark:text-red-300">
          {list(o.knockouts).map((k) => (
            <li key={k.rule}>
              Knocked out at Gate 2: {label(k.rule)}
              {k.detail ? ` — ${k.detail}` : ""}
            </li>
          ))}
        </ul>
      )}

      {problem && (
        <Section title="Problem">
          <Card>
            <div className="flex flex-wrap items-center gap-2">
              <p className="font-medium">{problem.name}</p>
              {problem.claim_id ? <ClaimChip id={problem.claim_id} claims={claims} /> : null}
            </div>
            <p className="mt-1 text-sm text-zinc-700 dark:text-zinc-300">{problem.description}</p>
            <div className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Stat label="Evidence strength">
                {problem.evidence_strength != null ? `${problem.evidence_strength.toFixed(2)}/10` : "–"}
              </Stat>
              <Stat label="Independent sources">{problem.independent_source_count}</Stat>
              <Stat label="Source mix">
                {Object.entries(record(problem.source_category_mix))
                  .map(([k, v]) => `${k} ${v}`)
                  .join(", ") || "–"}
              </Stat>
            </div>
          </Card>
        </Section>
      )}

      <Section title="Score factors">
        <Factors o={o} claims={claims} />
      </Section>

      {experiment && (
        <Section title="Validation experiment">
          <Card>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{experiment.name ? label(experiment.name) : "experiment"}</span>
              {experiment.factor && <span className="text-xs text-zinc-500">tests {label(experiment.factor)}</span>}
              <span className="text-xs text-zinc-500">
                {usd(experiment.cost_usd, 0)} · {experiment.duration_days ?? "–"} days
              </span>
            </div>
            {experiment.pass_fail && <p className="mt-1 text-sm">{experiment.pass_fail}</p>}
            {experiment.claim_id ? (
              <div className="mt-2">
                <ClaimChip id={experiment.claim_id} claims={claims} />
              </div>
            ) : null}
          </Card>
        </Section>
      )}

      <Section title="Economics">
        <Economics m={o.economic_model ?? {}} claims={claims} />
        {list(o.wtp_signals).length > 0 && (
          <div className="mt-3">
            <div className="mb-1 text-xs text-zinc-500">Willingness-to-pay signals</div>
            <ul className="space-y-1 text-sm">
              {list(o.wtp_signals).map((w, i) => (
                <li key={`${w.claim_id}-${i}`} className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-zinc-500">{w.kind ? label(w.kind) : "signal"}</span>
                  <span>{w.note}</span>
                  {w.claim_id ? <ClaimChip id={w.claim_id} claims={claims} /> : null}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      <Section title="Buyers">
        <Card className="py-1">
          <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
            {ROLES.map((name) => (
              <Role key={name} name={name} role={roles[name]} claims={claims} />
            ))}
          </ul>
        </Card>
        {list(roles.gap_claim_ids).length > 0 && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            <span className="text-xs text-zinc-500">Targets gaps</span>
            <ClaimList ids={roles.gap_claim_ids} claims={claims} />
          </div>
        )}
        <div className="mt-4">
          <div className="mb-1 text-xs text-zinc-500">Channels</div>
          {channels.length > 0 ? (
            <ul className="space-y-1 text-sm">
              {channels.map((c, i) => (
                <li key={`${c.name}-${i}`} className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-zinc-500">{c.kind}</span>
                  <span lang="tr">{c.name}</span>
                  {list(c.claim_ids).length > 0 ? (
                    <ClaimList ids={c.claim_ids} claims={claims} />
                  ) : (
                    <span className="text-xs text-zinc-400">uncited</span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-zinc-500">None.</p>
          )}
        </div>
        {o.market_breadth?.hint && (
          <p className="mt-3 text-sm text-zinc-600 dark:text-zinc-400">
            Market breadth ({o.market_breadth.status ?? "unknown"}): {o.market_breadth.hint}
          </p>
        )}
      </Section>

      <Section title="Competitors and gaps">
        {o.gap_matrix ? <Gaps matrix={o.gap_matrix} claims={claims} /> : <p className="text-sm text-zinc-500">No gap matrix.</p>}
      </Section>

      <Section title="Rule trace">
        <Trace trace={list(o.score?.rule_trace)} />
      </Section>
    </main>
  );
}
