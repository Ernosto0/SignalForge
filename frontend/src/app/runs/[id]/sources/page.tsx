import Link from "next/link";
import { notFound } from "next/navigation";

import { BackendDown, StatusBadge } from "@/components/ui";
import { BackendUnavailable, getSources, type Facets, type SourceFilters, type SourcePage } from "@/lib/api";
import { dateTime, label } from "@/lib/format";

const PAGE_SIZE = 50;
const FILTER_KEYS = ["category", "tier", "domain", "signal_type", "cluster_id", "q"] as const;
// These filters select excerpts: a matching document shows only its matching excerpts.
const EXCERPT_FILTERS = ["signal_type", "cluster_id", "q"] as const;
// The API reports documents without a source category as "unlisted".
const categoryLabel = (k: string) => (k === "unlisted" ? "unlisted domain" : label(k));

const input =
  "w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900";

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function FacetSelect({
  name,
  title,
  options,
  value,
}: {
  name: string;
  title: string;
  options: [string, string, number][]; // value, label, count
  value?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-zinc-500">{title}</span>
      <select name={name} defaultValue={value ?? ""} className={input}>
        <option value="">All</option>
        {options.map(([v, text, n]) => (
          <option key={v} value={v}>
            {text} ({n})
          </option>
        ))}
      </select>
    </label>
  );
}

function Filters({ facets, filters, runId }: { facets: Facets; filters: SourceFilters; runId: number }) {
  const entries = (m: Record<string, number>, name = label): [string, string, number][] =>
    Object.entries(m ?? {}).map(([k, n]) => [k, name(k), n]);
  return (
    <form method="get" className="grid gap-3 rounded-lg border border-zinc-200 bg-white p-4 sm:grid-cols-3 lg:grid-cols-6 dark:border-zinc-800 dark:bg-zinc-900">
      <label className="block sm:col-span-3 lg:col-span-2">
        <span className="mb-1 block text-xs font-medium text-zinc-500">Search quotes</span>
        <input
          name="q"
          defaultValue={filters.q}
          placeholder="Turkish quote text only, e.g. irsaliye"
          title="Matches the original Turkish quote, not the English translation"
          lang="tr"
          className={input}
        />
      </label>
      <FacetSelect name="category" title="Source category" options={entries(facets.categories, categoryLabel)} value={filters.category} />
      <FacetSelect name="tier" title="Quality tier" options={entries(facets.tiers)} value={filters.tier} />
      <FacetSelect name="signal_type" title="Signal type" options={entries(facets.signal_types)} value={filters.signal_type} />
      <FacetSelect
        name="cluster_id"
        title="Problem cluster"
        options={(facets.clusters ?? []).map((c) => [String(c.id), c.name, c.n])}
        value={filters.cluster_id}
      />
      <label className="block lg:col-span-2">
        <span className="mb-1 block text-xs font-medium text-zinc-500">Domain</span>
        <input name="domain" defaultValue={filters.domain} placeholder="e.g. sikayetvar.com" className={input} />
      </label>
      <div className="flex items-end gap-3 sm:col-span-2 lg:col-span-4">
        <button type="submit" className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900">
          Filter
        </button>
        <Link href={`/runs/${runId}/sources`} className="text-sm text-zinc-500 hover:underline">
          Clear
        </Link>
      </div>
    </form>
  );
}

function Results({ page, clusters }: { page: SourcePage; clusters: Map<number, string> }) {
  if (page.items.length === 0) return <p className="text-sm text-zinc-500">No documents match.</p>;
  return (
    <ul className="space-y-4">
      {page.items.map(({ document: d, excerpts }) => (
        <li key={d.id} className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
          <div className="flex flex-wrap items-center gap-2">
            <a href={d.url} target="_blank" rel="noreferrer" className="min-w-0 truncate font-medium hover:underline">
              {d.title || d.url}
            </a>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
            <span>{d.domain}</span>
            <span>· {d.source_category ? label(d.source_category) : "unlisted domain"}</span>
            {d.quality_tier && <span>· {d.quality_tier} tier</span>}
            {d.published_at && <span>· {dateTime(d.published_at).slice(0, 10)}</span>}
            {d.snippet_only && <StatusBadge value="snippet only" />}
            {d.origin && d.origin !== "collect" && <StatusBadge value={d.origin} />}
          </div>
          <ul className="mt-3 space-y-3">
            {excerpts.map((e) => (
              <li key={e.id} className="rounded-md bg-zinc-50 p-3 text-sm dark:bg-zinc-950">
                <p lang="tr">„{e.quote}”</p>
                {e.translation && <p className="mt-1 italic text-zinc-500">{e.translation}</p>}
                <div className="mt-1 text-xs text-zinc-500">
                  verified: <span className={e.verified === "fuzzy" ? "text-amber-600" : ""}>{e.verified}</span>
                </div>
                {e.signals.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {e.signals.map((s) => (
                      <li key={s.id} className="flex flex-wrap items-center gap-1.5 text-xs">
                        <StatusBadge value={s.type} />
                        {s.first_hand && <span className="text-emerald-600">first-hand</span>}
                        <span className="text-zinc-700 dark:text-zinc-300">{s.statement}</span>
                        {s.cluster_ids.map((c) => (
                          <span key={c} className="rounded border border-zinc-300 px-1 text-zinc-500 dark:border-zinc-700">
                            {clusters.get(c) ?? `cluster ${c}`}
                          </span>
                        ))}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

export default async function SourcesPage(props: PageProps<"/runs/[id]/sources">) {
  const { id } = await props.params;
  const runId = Number(id);
  if (!Number.isInteger(runId)) notFound();
  const search = await props.searchParams;
  const filters: SourceFilters = {};
  for (const key of FILTER_KEYS) {
    const value = first(search[key])?.trim();
    if (value) filters[key] = value;
  }
  const offset = Math.max(Number(first(search.offset)) || 0, 0);

  let page: SourcePage | null;
  try {
    page = await getSources(runId, { ...filters, offset, limit: PAGE_SIZE });
  } catch (err) {
    if (!(err instanceof BackendUnavailable)) throw err;
    return (
      <main className="mx-auto w-full max-w-5xl px-4 py-8">
        <BackendDown />
      </main>
    );
  }
  if (!page) notFound();

  const clusters = new Map((page.facets.clusters ?? []).map((c) => [c.id, c.name]));
  const href = (newOffset: number) => {
    const params = new URLSearchParams(filters as Record<string, string>);
    if (newOffset > 0) params.set("offset", String(newOffset));
    const query = params.toString();
    return `/runs/${runId}/sources${query ? `?${query}` : ""}`;
  };
  const end = Math.min(page.offset + page.items.length, page.total);
  const excerptFiltered = EXCERPT_FILTERS.some((k) => filters[k]);

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8">
      <p className="text-sm text-zinc-500">
        <Link href="/" className="hover:underline">
          Runs
        </Link>{" "}
        /{" "}
        <Link href={`/runs/${runId}`} className="hover:underline">
          #{runId}
        </Link>{" "}
        / sources
      </p>
      <h1 className="mt-1 mb-4 text-xl font-semibold">Sources</h1>
      <Filters facets={page.facets} filters={filters} runId={runId} />
      <div className="my-4 flex items-center justify-between text-sm text-zinc-500">
        <span>
          {page.total === 0 ? "0 documents" : `${page.offset + 1}–${end} of ${page.total} documents`}
        </span>
        <span className="flex gap-3">
          {page.offset > 0 && (
            <Link href={href(Math.max(page.offset - page.limit, 0))} className="hover:underline">
              ← Prev
            </Link>
          )}
          {end < page.total && (
            <Link href={href(page.offset + page.limit)} className="hover:underline">
              Next →
            </Link>
          )}
        </span>
      </div>
      {excerptFiltered && (
        <p className="mb-3 text-xs text-zinc-500">
          Signal type, cluster and search filters select excerpts: each document shows only its
          matching excerpts.
        </p>
      )}
      <Results page={page} clusters={clusters} />
    </main>
  );
}
