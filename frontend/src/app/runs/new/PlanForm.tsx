"use client";

// The research form: edits a ResearchPlan directly (the planner is out of scope), seeded from an
// example plan. Lists are edited one item per line. 422 errors are shown under their field.
import { useRouter } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";

import { ApiError, createRun, type PlanExample, type ResearchPlan, type PlanSubmarket, type ValidationIssue } from "@/lib/api";

const input =
  "w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:focus:border-zinc-400";

const EMPTY_SUBMARKET: PlanSubmarket = { name: "", name_tr: "", terms: [], actors: [], notes: null };

const BLANK_PLAN: ResearchPlan = {
  request: {
    country: "TR",
    industry: "",
    problem_area: null,
    target_customer: "",
    business_model: "B2B SaaS",
    founder: { team: "", budget: "", mvp_months: 3, preferred_tech: [], min_customer_value_usd_month: 50 },
    report_language: "en",
    budget_usd: 10,
  },
  submarkets: [{ ...EMPTY_SUBMARKET }],
  research_questions: [],
  buyer_hypotheses: [],
  avoid: [],
};

/** Fill fields an older example file may lack, so every input is controlled. */
function normalize(plan: ResearchPlan): ResearchPlan {
  const r = plan.request ?? BLANK_PLAN.request;
  return {
    request: {
      ...BLANK_PLAN.request,
      ...r,
      founder: { ...BLANK_PLAN.request.founder, ...(r.founder ?? {}) },
    },
    submarkets: (plan.submarkets?.length ? plan.submarkets : [EMPTY_SUBMARKET]).map((s) => ({
      ...EMPTY_SUBMARKET,
      ...s,
      terms: s.terms ?? [],
      actors: s.actors ?? [],
    })),
    research_questions: plan.research_questions ?? [],
    buyer_hypotheses: plan.buyer_hypotheses ?? [],
    avoid: plan.avoid ?? [],
  };
}

const lines = (text: string) => text.split("\n");
const clean = (items: string[]) => items.map((s) => s.trim()).filter(Boolean);

/** Trim list items and drop empty lines before sending. */
function toPayload(plan: ResearchPlan): ResearchPlan {
  const r = plan.request;
  return {
    request: {
      ...r,
      problem_area: r.problem_area?.trim() || null,
      founder: { ...r.founder, preferred_tech: clean(r.founder.preferred_tech) },
    },
    submarkets: plan.submarkets.map((s) => ({
      ...s,
      terms: clean(s.terms),
      actors: clean(s.actors),
      notes: s.notes?.trim() || null,
    })),
    research_questions: clean(plan.research_questions),
    buyer_hypotheses: clean(plan.buyer_hypotheses),
    avoid: clean(plan.avoid),
  };
}

function Field({ label, error, children, hint }: { label: string; error?: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-zinc-600 dark:text-zinc-400">{label}</span>
      {children}
      {hint && !error && <span className="mt-0.5 block text-xs text-zinc-400">{hint}</span>}
      {error && <span className="mt-0.5 block text-xs text-red-600 dark:text-red-400">{error}</span>}
    </label>
  );
}

function Fieldset({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <legend className="px-1 text-sm font-semibold">{title}</legend>
      <div className="grid gap-3 sm:grid-cols-2">{children}</div>
    </fieldset>
  );
}

export function PlanForm({ examples }: { examples: PlanExample[] }) {
  const router = useRouter();
  const [template, setTemplate] = useState(examples[0]?.id ?? "");
  const [plan, setPlan] = useState<ResearchPlan>(() => normalize(examples[0]?.plan ?? BLANK_PLAN));
  const [start, setStart] = useState(true);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [general, setGeneral] = useState<string[]>([]);

  const r = plan.request;
  const setRequest = (patch: Partial<ResearchPlan["request"]>) => setPlan((p) => ({ ...p, request: { ...p.request, ...patch } }));
  const setFounder = (patch: Partial<ResearchPlan["request"]["founder"]>) =>
    setPlan((p) => ({ ...p, request: { ...p.request, founder: { ...p.request.founder, ...patch } } }));
  const setSubmarket = (i: number, patch: Partial<PlanSubmarket>) =>
    setPlan((p) => ({ ...p, submarkets: p.submarkets.map((s, j) => (j === i ? { ...s, ...patch } : s)) }));
  const err = (path: string) => errors[path];

  function pickTemplate(id: string) {
    setTemplate(id);
    const example = examples.find((e) => e.id === id);
    setPlan(normalize(example ? structuredClone(example.plan) : BLANK_PLAN));
    setErrors({});
    setGeneral([]);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setGeneral([]);
    try {
      const run = await createRun(toPayload(plan), start);
      router.push(`/runs/${run.id}`);
    } catch (error) {
      setBusy(false);
      if (error instanceof ApiError && Array.isArray(error.detail)) {
        const byField: Record<string, string> = {};
        const rest: string[] = [];
        for (const issue of error.detail as ValidationIssue[]) {
          const path = (issue.loc ?? []).filter((p) => p !== "body").join(".");
          if (path) byField[path] = issue.msg;
          else rest.push(issue.msg);
        }
        setErrors(byField);
        // Paths without an input on this form (e.g. a whole object) are listed at the top too.
        const known = new Set(Array.from(document.querySelectorAll("[data-path]")).map((el) => el.getAttribute("data-path")));
        setGeneral([...rest, ...Object.entries(byField).filter(([p]) => !known.has(p)).map(([p, m]) => `${p}: ${m}`)]);
      } else {
        setGeneral([error instanceof Error ? error.message : String(error)]);
      }
    }
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Template">
          <select className={input} value={template} onChange={(e) => pickTemplate(e.target.value)}>
            {examples.map((x) => (
              <option key={x.id} value={x.id}>
                {x.title}
              </option>
            ))}
            <option value="">Blank plan</option>
          </select>
        </Field>
      </div>

      {general.length > 0 && (
        <ul className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-red-600/20 dark:bg-red-500/10 dark:text-red-300">
          {general.map((g, i) => (
            <li key={i}>{g}</li>
          ))}
        </ul>
      )}

      <Fieldset title="Request">
        <Field label="Country (market pack)" error={err("request.country")}>
          <input data-path="request.country" className={input} value={r.country} onChange={(e) => setRequest({ country: e.target.value })} />
        </Field>
        <Field label="Industry" error={err("request.industry")}>
          <input data-path="request.industry" className={input} value={r.industry} onChange={(e) => setRequest({ industry: e.target.value })} />
        </Field>
        <Field label="Problem area (optional)" error={err("request.problem_area")}>
          <input data-path="request.problem_area" className={input} value={r.problem_area ?? ""} onChange={(e) => setRequest({ problem_area: e.target.value })} />
        </Field>
        <Field label="Target customer" error={err("request.target_customer")}>
          <input data-path="request.target_customer" className={input} value={r.target_customer} onChange={(e) => setRequest({ target_customer: e.target.value })} />
        </Field>
        <Field label="Business model" error={err("request.business_model")}>
          <input data-path="request.business_model" className={input} value={r.business_model} onChange={(e) => setRequest({ business_model: e.target.value })} />
        </Field>
        <Field label="Report language" error={err("request.report_language")} hint="Reports are English-only for now">
          <input data-path="request.report_language" className={input} value={r.report_language} onChange={(e) => setRequest({ report_language: e.target.value })} />
        </Field>
        <Field label="Budget (USD)" error={err("request.budget_usd")}>
          <input
            data-path="request.budget_usd"
            type="number"
            min={0}
            step="0.5"
            className={input}
            value={Number.isFinite(r.budget_usd) ? r.budget_usd : ""}
            onChange={(e) => setRequest({ budget_usd: e.target.valueAsNumber })}
          />
        </Field>
      </Fieldset>

      <Fieldset title="Founder">
        <Field label="Team" error={err("request.founder.team")}>
          <input data-path="request.founder.team" className={input} value={r.founder.team} onChange={(e) => setFounder({ team: e.target.value })} />
        </Field>
        <Field label="Budget" error={err("request.founder.budget")}>
          <input data-path="request.founder.budget" className={input} value={r.founder.budget} onChange={(e) => setFounder({ budget: e.target.value })} />
        </Field>
        <Field label="MVP months" error={err("request.founder.mvp_months")}>
          <input
            data-path="request.founder.mvp_months"
            type="number"
            min={1}
            className={input}
            value={Number.isFinite(r.founder.mvp_months) ? r.founder.mvp_months : ""}
            onChange={(e) => setFounder({ mvp_months: e.target.valueAsNumber })}
          />
        </Field>
        <Field label="Minimum customer value (USD / month)" error={err("request.founder.min_customer_value_usd_month")}>
          <input
            data-path="request.founder.min_customer_value_usd_month"
            type="number"
            min={0}
            className={input}
            value={Number.isFinite(r.founder.min_customer_value_usd_month) ? r.founder.min_customer_value_usd_month : ""}
            onChange={(e) => setFounder({ min_customer_value_usd_month: e.target.valueAsNumber })}
          />
        </Field>
        <Field label="Preferred tech (one per line)" error={err("request.founder.preferred_tech")}>
          <textarea
            data-path="request.founder.preferred_tech"
            rows={2}
            className={input}
            value={r.founder.preferred_tech.join("\n")}
            onChange={(e) => setFounder({ preferred_tech: lines(e.target.value) })}
          />
        </Field>
      </Fieldset>

      <fieldset className="space-y-3">
        <legend className="mb-2 text-sm font-semibold">Submarkets</legend>
        {err("submarkets") && <p className="text-xs text-red-600 dark:text-red-400">{err("submarkets")}</p>}
        {plan.submarkets.map((s, i) => (
          <div key={i} className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium text-zinc-500">Submarket {i + 1}</span>
              <button
                type="button"
                className="text-xs text-red-600 hover:underline disabled:opacity-40"
                disabled={plan.submarkets.length === 1}
                onClick={() => setPlan((p) => ({ ...p, submarkets: p.submarkets.filter((_, j) => j !== i) }))}
              >
                Remove
              </button>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Name (English)" error={err(`submarkets.${i}.name`)}>
                <input data-path={`submarkets.${i}.name`} className={input} value={s.name} onChange={(e) => setSubmarket(i, { name: e.target.value })} />
              </Field>
              <Field label="Name (Turkish)" error={err(`submarkets.${i}.name_tr`)}>
                <input data-path={`submarkets.${i}.name_tr`} lang="tr" className={input} value={s.name_tr} onChange={(e) => setSubmarket(i, { name_tr: e.target.value })} />
              </Field>
              <Field label="Terms (one per line)" error={err(`submarkets.${i}.terms`)}>
                <textarea data-path={`submarkets.${i}.terms`} lang="tr" rows={4} className={input} value={s.terms.join("\n")} onChange={(e) => setSubmarket(i, { terms: lines(e.target.value) })} />
              </Field>
              <Field label="Actors (one per line)" error={err(`submarkets.${i}.actors`)}>
                <textarea data-path={`submarkets.${i}.actors`} lang="tr" rows={4} className={input} value={s.actors.join("\n")} onChange={(e) => setSubmarket(i, { actors: lines(e.target.value) })} />
              </Field>
              <div className="sm:col-span-2">
                <Field label="Notes" error={err(`submarkets.${i}.notes`)}>
                  <textarea data-path={`submarkets.${i}.notes`} rows={2} className={input} value={s.notes ?? ""} onChange={(e) => setSubmarket(i, { notes: e.target.value })} />
                </Field>
              </div>
            </div>
          </div>
        ))}
        <button
          type="button"
          className="rounded-md border border-dashed border-zinc-300 px-3 py-1.5 text-sm hover:bg-white dark:border-zinc-700 dark:hover:bg-zinc-900"
          onClick={() => setPlan((p) => ({ ...p, submarkets: [...p.submarkets, { ...EMPTY_SUBMARKET }] }))}
        >
          Add submarket
        </button>
      </fieldset>

      <Fieldset title="Research focus (one per line)">
        <Field label="Research questions" error={err("research_questions")}>
          <textarea data-path="research_questions" rows={4} className={input} value={plan.research_questions.join("\n")} onChange={(e) => setPlan((p) => ({ ...p, research_questions: lines(e.target.value) }))} />
        </Field>
        <Field label="Buyer hypotheses" error={err("buyer_hypotheses")}>
          <textarea data-path="buyer_hypotheses" rows={4} className={input} value={plan.buyer_hypotheses.join("\n")} onChange={(e) => setPlan((p) => ({ ...p, buyer_hypotheses: lines(e.target.value) }))} />
        </Field>
        <Field label="Avoid" error={err("avoid")}>
          <textarea data-path="avoid" rows={3} className={input} value={plan.avoid.join("\n")} onChange={(e) => setPlan((p) => ({ ...p, avoid: lines(e.target.value) }))} />
        </Field>
      </Fieldset>

      <div className="flex flex-wrap items-center gap-4">
        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {busy ? "Creating…" : start ? "Create and start" : "Create run"}
        </button>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={start} onChange={(e) => setStart(e.target.checked)} />
          Start the pipeline now (spends up to the budget)
        </label>
      </div>
    </form>
  );
}
