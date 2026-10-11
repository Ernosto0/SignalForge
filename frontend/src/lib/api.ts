// API contract with the FastAPI backend (.claude/m8-spec.md, "Backend > B2").
// Server components call FastAPI directly (BACKEND_URL); client components call "/api/*", which
// next.config.ts proxies to the same backend (no CORS).
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

// --- shapes -----------------------------------------------------------------------------------

export type Health = {
  status: "ok" | "degraded";
  version: string;
  database: boolean;
  llm_key_configured: boolean;
  search_key_configured: boolean;
};

export type JobStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export type JobArgs = {
  resume?: boolean;
  from_stage?: string | null;
  until?: string | null;
  agent?: string | null;
};

export type Job = {
  id: number;
  run_id: number;
  status: JobStatus;
  args: JobArgs;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
};

export type RunSummary = {
  id: number;
  created_at: string;
  updated_at: string;
  status: string;
  country: string;
  industry: string;
  problem_area?: string | null;
  budget_usd: number;
  spent_usd: number;
  current_stage?: string | null;
  stages_done: number;
  stages_total: number;
  latest_job?: Job | null;
  opportunity_count: number;
};

export type StageStatus = "pending" | "running" | "completed" | "failed";

export type StageOut = {
  name: string;
  status: StageStatus;
  started_at?: string | null;
  finished_at?: string | null;
  cost_usd: number;
  error?: string | null;
  metrics: Record<string, unknown>;
};

export type AgentProgress = {
  name: string;
  description: string;
  status: StageStatus | "partial";
  cost_usd: number;
  started_at?: string | null;
  finished_at?: string | null;
  stages: StageOut[];
};

export type ReportFormat = "json" | "md" | "html";

export type RunDetail = RunSummary & {
  plan: ResearchPlan | null;
  pack_id: string;
  pack_version: string;
  agents: AgentProgress[];
  jobs: Job[]; // newest first
  report_formats: ReportFormat[];
};

export type AgentInfo = { name: string; description: string; stages: string[] };

export type FounderProfile = {
  team: string;
  budget: string;
  mvp_months: number;
  preferred_tech: string[];
  min_customer_value_usd_month: number;
};

export type ResearchRequest = {
  country: string;
  industry: string;
  problem_area?: string | null;
  target_customer: string;
  business_model: string;
  founder: FounderProfile;
  report_language: string;
  budget_usd: number;
};

export type PlanSubmarket = {
  name: string;
  name_tr: string;
  terms: string[];
  actors: string[];
  notes?: string | null;
};

export type ResearchPlan = {
  request: ResearchRequest;
  submarkets: PlanSubmarket[];
  research_questions: string[];
  buyer_hypotheses: string[];
  avoid: string[];
};

export type PlanExample = { id: string; title: string; plan: ResearchPlan };

export type ValueRange = { low: number; high: number };

export type OpportunityCard = {
  id: number;
  run_id: number;
  problem_id: number;
  problem_name: string;
  segment: string;
  solution_angle: string;
  gate2_status?: string | null; // passed | knocked_out
  knockouts: { rule: string; detail?: string | null }[];
  category?: string | null;
  attractiveness?: number | null;
  confidence?: string | null;
  founder_fit?: string | null;
  evidence_strength?: number | null;
  value_usd_month?: ValueRange | null;
};

export type ExcerptRef = {
  id: number;
  quote: string;
  translation?: string | null;
  verified: string; // exact | fuzzy
  url: string;
  domain: string;
  source_category?: string | null;
  quality_tier?: string | null;
};

export type ClaimOut = {
  id: number;
  kind: string; // fact | inference | hypothesis | assumption
  statement: string;
  entailment?: string | null;
  derived_from?: number[]; // one level down; those claims are in the same map
  excerpts: ExcerptRef[];
};

export type ClaimMap = Record<string, ClaimOut>;

// Stored JSON blobs are passed through as written by the stages; every field may be missing on
// older runs, so the types are loose and the UI reads them defensively.
export type RoleClaim = {
  role?: string;
  claim_ids?: number[];
  hypothesis?: string | null;
  hypothesis_claim_id?: number | null;
  entailment?: string | null;
};

export type BuyerRoles = Partial<
  Record<"user" | "buyer" | "decision_maker" | "economic_beneficiary", RoleClaim>
> & { budget_owner?: RoleClaim | null; gap_claim_ids?: number[] };

export type Channel = { kind?: string; name?: string; claim_ids?: number[]; cited?: boolean };

export type EconomicModel = {
  status?: string;
  formula?: string;
  currency?: string;
  inputs?: Record<string, number>; // input name -> assumption claim id
  value_local?: [number, number];
  value_usd_month?: [number, number];
  capture_share?: [number, number];
  price_ceiling_usd_month?: [number, number];
  competitor_anchor_usd_month?: { n?: number; min?: number; median?: number; claim_ids?: number[] };
  fx?: { rate?: number; as_of?: string; source_url?: string };
  [key: string]: unknown;
};

export type Factor = {
  level?: number;
  weight?: number;
  justification?: string;
  claim_ids?: number[];
  source?: string; // model | code
  capped?: boolean;
  judges?: number[];
  spread?: number;
  [key: string]: unknown;
};

export type TraceEntry = {
  rule?: string;
  fired?: boolean;
  result?: unknown;
  factor?: string;
  inputs?: Record<string, unknown>;
  [key: string]: unknown;
};

export type Experiment = {
  name?: string;
  claim_id?: number;
  factor?: string;
  pass_fail?: string;
  cost_usd?: number;
  duration_days?: number;
  importance?: number;
  uncertainty?: number;
  score?: number;
};

export type GapCell = { value?: string; claim_id?: number | null };

export type GapMatrix = {
  dimensions: { key: string; label?: string; from_signal_ids?: number[] }[];
  competitors: { id: number; name: string; url?: string | null }[];
  cells: Record<string, Record<string, GapCell>>; // dimension key -> competitor id -> cell
  gaps: { dimension: string; claim_id?: number | null }[];
};

export type OpportunityDetail = OpportunityCard & {
  problem: {
    id: number;
    name: string;
    description: string;
    independent_source_count: number;
    evidence_strength?: number | null;
    source_category_mix: Record<string, number>;
    claim_id?: number | null; // the problem's own inference claim (in `claims`)
  };
  buyer_roles: BuyerRoles;
  accessibility: { channels?: Channel[] };
  market_breadth: { hint?: string | null; status?: string };
  economic_model: EconomicModel;
  wtp_signals: { kind?: string; note?: string; claim_id?: number }[];
  score?: { factors: Record<string, Factor>; rule_trace: TraceEntry[]; experiment?: Experiment | null } | null;
  gap_matrix?: GapMatrix | null;
  claims: ClaimMap;
};

export type SourceSignal = {
  id: number;
  type: string;
  statement: string;
  first_hand: boolean;
  cluster_ids: number[];
};

export type SourceItem = {
  document: {
    id: number;
    url: string;
    domain: string;
    title?: string | null;
    source_category?: string | null;
    quality_tier?: string | null;
    published_at?: string | null;
    snippet_only: boolean;
    origin?: string | null;
  };
  excerpts: {
    id: number;
    quote: string;
    translation?: string | null;
    verified: string;
    signals: SourceSignal[];
  }[];
};

export type Facets = {
  categories: Record<string, number>;
  tiers: Record<string, number>;
  signal_types: Record<string, number>;
  clusters: { id: number; name: string; n: number }[];
};

export type SourcePage = {
  total: number;
  offset: number;
  limit: number;
  items: SourceItem[];
  facets: Facets;
};

export type SourceFilters = {
  category?: string;
  tier?: string;
  domain?: string;
  signal_type?: string;
  cluster_id?: string;
  q?: string;
  offset?: number;
  limit?: number;
};

export function hasActiveJob(job?: Job | null): boolean {
  return job?.status === "queued" || job?.status === "running";
}

// --- server side (server components) ----------------------------------------------------------

export class BackendUnavailable extends Error {}

/** GET a JSON resource from FastAPI. `null` on 404; throws BackendUnavailable when it is down. */
async function serverGet<T>(path: string): Promise<T | null> {
  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}${path}`, { cache: "no-store" });
  } catch {
    throw new BackendUnavailable(`API unreachable at ${BACKEND_URL}`);
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status} ${await res.text()}`);
  return (await res.json()) as T;
}

export async function getHealth(): Promise<Health | null> {
  try {
    return await serverGet<Health>("/api/health");
  } catch {
    return null;
  }
}

export const getRuns = (limit = 50) => serverGet<RunSummary[]>(`/api/runs?limit=${limit}`);
export const getRun = (id: number) => serverGet<RunDetail>(`/api/runs/${id}`);
export const getAgents = () => serverGet<AgentInfo[]>("/api/agents");
export const getPlanExamples = () => serverGet<PlanExample[]>("/api/plans/examples");
export const getRunOpportunities = (id: number) =>
  serverGet<OpportunityCard[]>(`/api/runs/${id}/opportunities`);
export const getOpportunity = (id: number) =>
  serverGet<OpportunityDetail>(`/api/opportunities/${id}`);

export function getSources(runId: number, filters: SourceFilters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const query = params.toString();
  return serverGet<SourcePage>(`/api/runs/${runId}/sources${query ? `?${query}` : ""}`);
}

/** Link for the browser (proxied by Next.js). */
export const reportUrl = (runId: number, fmt: ReportFormat) => `/api/runs/${runId}/report/${fmt}`;

// --- client side (client components) ----------------------------------------------------------

export type ValidationIssue = { loc: (string | number)[]; msg: string; type?: string };

/** A non-2xx answer: `detail` is FastAPI's error body (a string, or 422 validation issues). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string | ValidationIssue[] | unknown,
  ) {
    super(typeof detail === "string" ? detail : `request failed (${status})`);
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? body ?? res.statusText);
  return body as T;
}

export const createRun = (plan: ResearchPlan, start = true) =>
  apiFetch<RunDetail>(`/api/runs?start=${start}`, { method: "POST", body: JSON.stringify(plan) });

export const enqueueJob = (runId: number, args: JobArgs) =>
  apiFetch<Job>(`/api/runs/${runId}/jobs`, { method: "POST", body: JSON.stringify(args) });

export const cancelJob = (jobId: number) =>
  apiFetch<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" });
