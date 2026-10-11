"use client";

// Re-renders the current server page every `ms` while `active` (a job is queued or running) and
// the tab is visible. The server decides `active` on each render, so polling stops by itself.
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export function AutoRefresh({ active, ms }: { active: boolean; ms: number }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") router.refresh();
    }, ms);
    return () => clearInterval(timer);
  }, [active, ms, router]);
  return active ? (
    <span className="inline-flex items-center gap-1.5 text-xs text-zinc-500">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
      live
    </span>
  ) : null;
}
