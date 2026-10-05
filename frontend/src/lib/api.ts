// Server-side calls go straight to FastAPI; client components should call "/api/*" (proxied).
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export type Health = {
  status: "ok" | "degraded";
  version: string;
  database: boolean;
  llm_key_configured: boolean;
  search_key_configured: boolean;
};

export async function getHealth(): Promise<Health | null> {
  try {
    const res = await fetch(`${BACKEND_URL}/api/health`);
    return res.ok ? ((await res.json()) as Health) : null;
  } catch {
    return null;
  }
}
