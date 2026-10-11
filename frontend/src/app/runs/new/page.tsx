import Link from "next/link";
import { connection } from "next/server";

import { BackendDown } from "@/components/ui";
import { BackendUnavailable, getPlanExamples, type PlanExample } from "@/lib/api";

import { PlanForm } from "./PlanForm";

export default async function NewRunPage() {
  await connection();
  let examples: PlanExample[] | null;
  try {
    examples = await getPlanExamples();
  } catch (err) {
    if (!(err instanceof BackendUnavailable)) throw err;
    return (
      <main className="mx-auto w-full max-w-4xl px-4 py-8">
        <BackendDown />
      </main>
    );
  }

  return (
    <main className="mx-auto w-full max-w-4xl px-4 py-8">
      <p className="text-sm text-zinc-500">
        <Link href="/" className="hover:underline">
          Runs
        </Link>{" "}
        / new
      </p>
      <h1 className="mt-1 mb-1 text-xl font-semibold">New research</h1>
      <p className="mb-6 text-sm text-zinc-500">
        Start from a template plan, adjust the request and submarkets, then create the run.
      </p>
      <PlanForm examples={examples ?? []} />
    </main>
  );
}
