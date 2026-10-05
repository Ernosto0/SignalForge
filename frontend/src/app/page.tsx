import { connection } from "next/server";

import { getHealth } from "@/lib/api";

function Check({ label, ok }: { label: string; ok: boolean }) {
  return (
    <li className="flex items-center justify-between border-b border-zinc-200 py-2 dark:border-zinc-800">
      <span>{label}</span>
      <span className={ok ? "text-emerald-600" : "text-amber-600"}>{ok ? "ok" : "missing"}</span>
    </li>
  );
}

export default async function Home() {
  await connection();
  const health = await getHealth();

  return (
    <main className="mx-auto w-full max-w-xl px-4 py-16">
      <h1 className="text-2xl font-semibold">SignalForge</h1>
      <p className="mt-1 text-zinc-500">Evidence first. Ideas second.</p>

      <h2 className="mt-10 mb-2 text-sm font-medium uppercase tracking-wide text-zinc-500">
        Setup status
      </h2>
      {health ? (
        <ul>
          <Check label={`API (v${health.version})`} ok />
          <Check label="Database" ok={health.database} />
          <Check label="OpenAI API key" ok={health.llm_key_configured} />
          <Check label="Search API key" ok={health.search_key_configured} />
        </ul>
      ) : (
        <p className="text-amber-600">
          API unreachable. Start it with <code>uv run signalforge serve --reload</code> in{" "}
          <code>backend/</code>.
        </p>
      )}
    </main>
  );
}
