// Presentational building blocks (server-safe: no hooks).
import type { ReactNode } from "react";

const TONES = {
  green: "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-400/30",
  blue: "bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-300 dark:ring-sky-400/30",
  amber: "bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-400/30",
  red: "bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-500/10 dark:text-red-300 dark:ring-red-400/30",
  violet: "bg-violet-50 text-violet-700 ring-violet-600/20 dark:bg-violet-500/10 dark:text-violet-300 dark:ring-violet-400/30",
  gray: "bg-zinc-100 text-zinc-600 ring-zinc-500/20 dark:bg-zinc-800 dark:text-zinc-300 dark:ring-zinc-600/40",
} as const;

export type Tone = keyof typeof TONES;

const STATUS_TONE: Record<string, Tone> = {
  completed: "green",
  fired: "green",
  capped: "amber",
  passed: "green",
  fit: "green",
  strong: "green",
  supported: "green",
  high: "green",
  running: "blue",
  queued: "violet",
  interesting: "blue",
  medium: "blue",
  partial: "amber",
  stretch: "amber",
  competitive: "amber",
  stopped: "amber",
  paused_budget: "amber",
  low: "amber",
  fuzzy: "amber",
  failed: "red",
  knocked_out: "red",
  not_fit: "red",
  weak: "red",
  false_positive: "red",
  not_supported: "red",
  contradicted: "red",
  cancelled: "gray",
  pending: "gray",
  created: "gray",
  planned: "gray",
};

export function Badge({ children, tone = "gray", title }: { children: ReactNode; tone?: Tone; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center whitespace-nowrap rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

/** A badge coloured by a status / category / level word. */
export function StatusBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <Badge>–</Badge>;
  return <Badge tone={STATUS_TONE[value] ?? "gray"}>{value.replaceAll("_", " ")}</Badge>;
}

export function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="mt-8">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2 border-b border-zinc-200 pb-1 dark:border-zinc-800">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900 ${className}`}>
      {children}
    </div>
  );
}

export function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="truncate font-medium tabular-nums">{children}</div>
    </div>
  );
}

/** Pretty-printed JSON in a collapsed <details>. */
export function JsonBlock({ value, summary = "details" }: { value: unknown; summary?: string }) {
  return (
    <details className="group text-xs">
      <summary className="cursor-pointer select-none text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200">
        {summary}
      </summary>
      <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-zinc-50 p-3 text-[11px] leading-relaxed dark:bg-zinc-950">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

export function Notice({ children, tone = "amber" }: { children: ReactNode; tone?: Tone }) {
  return <div className={`rounded-md px-3 py-2 text-sm ring-1 ring-inset ${TONES[tone]}`}>{children}</div>;
}

export function BackendDown() {
  return (
    <Notice>
      API unreachable. Start it with <code>.venv/Scripts/signalforge serve --reload</code> in{" "}
      <code>backend/</code>.
    </Notice>
  );
}
