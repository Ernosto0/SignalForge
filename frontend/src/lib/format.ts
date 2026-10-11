// Small display helpers shared by the pages.
import type { Job } from "./api";

export function usd(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  return `$${value.toFixed(digits)}`;
}

export function range(r: [number, number] | { low: number; high: number } | null | undefined, unit = "", digits = 0): string {
  if (!r) return "–";
  const [low, high] = Array.isArray(r) ? r : [r.low, r.high];
  const fmt = (v: number) => v.toLocaleString("en-US", { maximumFractionDigits: digits });
  return `${unit}${fmt(low)}–${unit}${fmt(high)}`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(0, 16).replace("T", " ");
}

export function duration(start?: string | null, end?: string | null): string {
  if (!start) return "–";
  const ms = (end ? new Date(end) : new Date()).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "–";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function label(key: string): string {
  return key.replaceAll("_", " ");
}

/** A list from a blob field that may be missing or of the wrong type. */
export function list<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

/** A plain object from a blob field that may be missing or of the wrong type. */
export function record<T>(value: Record<string, T> | null | undefined): Record<string, T> {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export function pairOrNull(value: unknown): [number, number] | null {
  return Array.isArray(value) && value.length === 2 && value.every((v) => typeof v === "number")
    ? (value as [number, number])
    : null;
}

/** What a job runs, in a few words. */
export function jobLabel(job: Job): string {
  const a = job.args ?? {};
  if (a.agent) return `agent ${a.agent}`;
  const parts = [a.resume ? "resume" : "pipeline"];
  if (a.from_stage) parts.push(`from ${a.from_stage}`);
  if (a.until) parts.push(`until ${a.until}`);
  return parts.join(" ");
}
