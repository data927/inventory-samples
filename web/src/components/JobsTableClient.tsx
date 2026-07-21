"use client";

import { useEffect, useMemo, useState } from "react";
import { getApiBase } from "@/lib/apiBase";
import { JOBS_ACTIVE_EVENT } from "@/lib/inventoryEvents";

type Job = {
  id: string;
  type:
    | "build_inventory_from_dump"
    | "build_inventory_from_upload"
    | "build_inventory_from_repo"
    | "segment_inventory";
  status: "queued" | "running" | "succeeded" | "failed";
  createdAt: string;
  updatedAt: string;
  input?: Record<string, unknown>;
  outputPath?: string;
  error?: string;
};

function shortId(id: string) {
  const s = String(id || "");
  return s.length > 10 ? `${s.slice(0, 8)}...` : s;
}

function StatusPill({ status }: { status: Job["status"] }) {
  const cls =
    status === "succeeded"
      ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300"
      : status === "failed"
        ? "bg-rose-50 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300"
        : status === "running"
          ? "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
          : "bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300";

  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>
      {status === "running" ? (
        <span className="inline-block h-2 w-2 rounded-full bg-current opacity-70 animate-pulse" />
      ) : null}
      <span>{status}</span>
    </span>
  );
}

function typeBadge(type: Job["type"]) {
  if (type === "build_inventory_from_dump") return "folder";
  if (type === "build_inventory_from_upload") return "upload";
  if (type === "build_inventory_from_repo") return "repo";
  return "segment";
}

export default function JobsTableClient() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const sorted = useMemo(
    () => [...jobs].sort((a, b) => b.createdAt.localeCompare(a.createdAt)),
    [jobs],
  );

  async function refresh(): Promise<Job[] | null> {
    setError(null);
    setLoading(true);
    const res = await fetch(`${getApiBase()}/api/jobs`, { cache: "no-store" });
    if (!res.ok) {
      setError(`Failed to load jobs (${res.status})`);
      setLoading(false);
      return null;
    }
    const data = (await res.json()) as Job[];
    const list = Array.isArray(data) ? data : [];
    setJobs(list);
    setLoading(false);
    return list;
  }

  useEffect(() => {
    function onJobsActive() {
      setAutoRefresh(true);
    }
    window.addEventListener(JOBS_ACTIVE_EVENT, onJobsActive);
    return () => window.removeEventListener(JOBS_ACTIVE_EVENT, onJobsActive);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let kickoff: ReturnType<typeof setTimeout> | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    function hasActive(list: Job[]) {
      return list.some((j) => j.status === "queued" || j.status === "running");
    }

    async function loop() {
      if (cancelled) return;
      if (!autoRefresh) return;
      if (typeof document !== "undefined" && document.hidden) {
        timer = setTimeout(loop, 8000);
        return;
      }

      const list = await refresh();
      if (list === null) {
        timer = setTimeout(loop, 5000);
        return;
      }
      const active = hasActive(list);
      if (!active) {
        setAutoRefresh(false);
        return;
      }
      timer = setTimeout(loop, 3000);
    }

    kickoff = setTimeout(() => void loop(), 0);
    return () => {
      cancelled = true;
      if (kickoff) clearTimeout(kickoff);
      if (timer) clearTimeout(timer);
    };
  }, [autoRefresh]);

  return (
    <div className="grid gap-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs text-zinc-500 dark:text-zinc-400">
          {autoRefresh ? "Auto-refreshing while jobs run" : "Auto-refresh paused"}
          {loading ? " - updating..." : ""}.
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          className="h-8 rounded-lg border border-zinc-200 dark:border-zinc-800 px-3 text-xs font-semibold hover:bg-zinc-50 dark:hover:bg-zinc-900"
        >
          Refresh
        </button>
      </div>

      {error ? <div className="text-sm text-rose-700 dark:text-rose-300">{error}</div> : null}

      <table className="min-w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400 border-b border-zinc-200/70 dark:border-zinc-800/70">
          <tr>
            <th className="py-2 pr-4">Job</th>
            <th className="py-2 pr-4">Status</th>
            <th className="py-2 pr-4">Type</th>
            <th className="py-2 pr-4">Created</th>
            <th className="py-2 pr-4">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-200/70 dark:divide-zinc-800/70">
          {sorted.length === 0 ? (
            <tr>
              <td className="py-6 text-zinc-500 dark:text-zinc-400" colSpan={5}>
                {loading ? "Loading jobs..." : "No jobs yet. Start a run on the left."}
              </td>
            </tr>
          ) : (
            sorted.map((j) => (
              <tr key={j.id} className="align-top hover:bg-zinc-50/70 dark:hover:bg-zinc-900/30">
                <td className="py-3 pr-4 text-zinc-600 dark:text-zinc-400">
                  <span className="font-mono text-xs">{shortId(j.id)}</span>
                </td>
                <td className="py-3 pr-4">
                  <StatusPill status={j.status} />
                </td>
                <td className="py-3 pr-4 font-medium text-zinc-900 dark:text-zinc-50">
                  <span className="inline-flex items-center gap-2">
                    <span className="rounded-md bg-zinc-100 px-2 py-0.5 text-[12px] text-zinc-800 dark:bg-zinc-900 dark:text-zinc-200">
                      {typeBadge(j.type)}
                    </span>
                    <span className="text-sm">{j.type.replaceAll("_", " ")}</span>
                  </span>
                </td>
                <td className="py-3 pr-4 text-zinc-600 dark:text-zinc-400">
                  {new Date(j.createdAt).toLocaleString()}
                </td>
                <td className="py-3 pr-4">
                  <div className="grid gap-2">
                    {j.status === "succeeded" && j.outputPath ? (
                      <a
                        href={`${getApiBase()}/api/jobs/${j.id}/download`}
                        className="inline-flex h-8 w-fit items-center justify-center rounded-lg bg-zinc-900 px-3 text-xs font-semibold text-white hover:opacity-90 dark:bg-zinc-100 dark:text-black"
                      >
                        Download
                      </a>
                    ) : (
                      <span className="text-zinc-400 dark:text-zinc-600 text-xs">--</span>
                    )}

                    {(j.outputPath || j.error) ? (
                      <details className="text-xs text-zinc-600 dark:text-zinc-400">
                        <summary className="cursor-pointer select-none hover:text-zinc-900 dark:hover:text-zinc-100">
                          Details
                        </summary>
                        <div className="mt-2 grid gap-2 rounded-xl border border-zinc-200/70 bg-white px-3 py-2 dark:border-zinc-800/70 dark:bg-zinc-950">
                          {j.input && (j.input["phase"] || j.input["queuePosition"] != null) ? (
                            <div className="grid gap-1">
                              <div className="text-[11px] uppercase tracking-wide text-zinc-500 dark:text-zinc-500">
                                Progress
                              </div>
                              <div className="text-xs text-zinc-800 dark:text-zinc-200">
                                {j.input["phase"] ? (
                                  <span className="font-medium">{String(j.input["phase"])}</span>
                                ) : (
                                  <span className="font-medium">queued</span>
                                )}
                                {j.input["queuePosition"] != null ? (
                                  <span className="text-zinc-500 dark:text-zinc-400">
                                    {" "}
                                    (position {String(j.input["queuePosition"])})
                                  </span>
                                ) : null}
                              </div>
                            </div>
                          ) : null}
                          {j.outputPath ? (
                            <div className="grid gap-1">
                              <div className="text-[11px] uppercase tracking-wide text-zinc-500 dark:text-zinc-500">
                                Output path
                              </div>
                              <code className="text-xs font-mono break-all text-zinc-800 dark:text-zinc-200">
                                {j.outputPath}
                              </code>
                            </div>
                          ) : null}
                          {j.error ? (
                            <div className="grid gap-1">
                              <div className="text-[11px] uppercase tracking-wide text-zinc-500 dark:text-zinc-500">
                                Error
                              </div>
                              <code className="text-xs text-rose-700 dark:text-rose-300 wrap-break-word">
                                {j.error}
                              </code>
                            </div>
                          ) : null}
                        </div>
                      </details>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

