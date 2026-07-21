"use client";

import { useEffect, useMemo, useRef, useState, type FormEvent, type InputHTMLAttributes } from "react";
import { getApiBase } from "@/lib/apiBase";
import { JOBS_ACTIVE_EVENT } from "@/lib/inventoryEvents";
import { parseJsonOrThrow } from "@/lib/parseJson";

function notifyJobsTableActive() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(JOBS_ACTIVE_EVENT));
}

async function readJsonOrText(res: Response): Promise<{ json: any | null; text: string }> {
  const ct = (res.headers.get("content-type") || "").toLowerCase();
  if (ct.includes("application/json")) {
    try {
      const j = await res.json();
      return { json: j, text: "" };
    } catch {
      // fall through
    }
  }
  const t = await res.text().catch(() => "");
  return { json: null, text: t };
}

type Mode = "dump" | "upload" | "repo" | "segment";

const Input =
  "h-10 w-full rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-black px-3 text-sm text-zinc-900 dark:text-zinc-50 placeholder:text-zinc-400 dark:placeholder:text-zinc-600 focus:outline-none focus:ring-2 focus:ring-zinc-900/10 dark:focus:ring-zinc-100/10";
const Select =
  "h-10 w-full rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-black px-3 text-sm text-zinc-900 dark:text-zinc-50 focus:outline-none focus:ring-2 focus:ring-zinc-900/10 dark:focus:ring-zinc-100/10";
const Label = "text-xs font-medium text-zinc-700 dark:text-zinc-300";
const Subtle = "text-sm text-zinc-600 dark:text-zinc-400";
const Primary =
  "h-10 rounded-xl bg-zinc-900 text-white dark:bg-zinc-100 dark:text-black text-sm font-semibold hover:opacity-90 disabled:opacity-50";
const Card =
  "rounded-2xl border border-zinc-200/70 dark:border-zinc-800/70 bg-white dark:bg-zinc-950 shadow-sm p-6";

type FileWithPath = File & { webkitRelativePath?: string };

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
  outputPath?: string;
  error?: string;
};

function fmtDuration(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
}

function fileKey(f: File): string {
  const wrp = (f as FileWithPath).webkitRelativePath;
  if (wrp) return `p:${wrp}`;
  return `f:${f.name}:${f.size}:${f.lastModified}`;
}

function appendUploadFiles(fd: FormData, files: File[]) {
  for (const f of files) {
    const wrp = (f as FileWithPath).webkitRelativePath;
    const filename =
      wrp && wrp.length > 0 ? wrp.replaceAll("\\", "/") : f.name || "file";
    fd.append("files", f, filename);
  }
}

export default function RunPanelClient() {
  const [mode, setMode] = useState<Mode>("dump");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [currentJobType, setCurrentJobType] = useState<Job["type"] | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [etaMs, setEtaMs] = useState<number | null>(null);
  const [stagedFiles, setStagedFiles] = useState<File[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const filePickerRef = useRef<HTMLInputElement>(null);
  const folderPickerRef = useRef<HTMLInputElement>(null);

  // Recover an in-flight job after reload/hot-reload.
  useEffect(() => {
    let cancelled = false;

    async function recover() {
      if (typeof window === "undefined") return;
      const stored = window.localStorage.getItem("inventory-intelligence:currentJobId") || "";
      if (!stored) return;
      // If we already have it in state, don't override.
      if (currentJobId) return;

      const res = await fetch(`${getApiBase()}/api/jobs`, { cache: "no-store" });
      if (!res.ok) return;
      const { json } = await readJsonOrText(res);
      const all = (json ?? []) as Job[];
      const found = Array.isArray(all) ? all.find((j) => j.id === stored) : null;
      const running = found && (found.status === "queued" || found.status === "running");
      if (!cancelled && running) {
        setCurrentJobId(found.id);
        setCurrentJobType(found.type);
        setJob(found);
      } else if (!cancelled) {
        window.localStorage.removeItem("inventory-intelligence:currentJobId");
      }
    }

    void recover();
    return () => {
      cancelled = true;
    };
  }, [currentJobId]);

  function mergeIncoming(incoming: FileList | File[] | null) {
    if (!incoming || (incoming instanceof FileList && incoming.length === 0)) return;
    const list = incoming instanceof FileList ? Array.from(incoming) : incoming;
    setStagedFiles((prev) => {
      const next = [...prev];
      const keys = new Set(next.map(fileKey));
      for (const f of list) {
        const k = fileKey(f);
        if (!keys.has(k)) {
          keys.add(k);
          next.push(f);
        }
      }
      return next;
    });
  }

  const title = useMemo(() => {
    if (mode === "dump") return "Build from folder path";
    if (mode === "upload") return "Build from uploaded files";
    if (mode === "repo") return "Build from repository";
    return "Segment inventory spreadsheet";
  }, [mode]);

  const description = useMemo(() => {
    if (mode === "dump") {
      return "Scan a folder path that exists on the server and export a clean workbook with Evidence and bucket tabs.";
    }
    if (mode === "upload") {
      return "Select many files from your computer, upload them, and download the output workbook.";
    }
    if (mode === "repo") {
      return "Download a GitHub/GitLab repo archive (token optional) and export the same clean workbook.";
    }
    return "Classify an existing .xlsx/.csv inventory and export Master plus bucket tabs plus Summary.";
  }, [mode]);

  useEffect(() => {
    if (!currentJobId) return;

    let cancelled = false;
    let poll: ReturnType<typeof setInterval> | null = null;
    let tick: ReturnType<typeof setInterval> | null = null;

    async function refreshJob() {
      const res = await fetch(`${getApiBase()}/api/jobs`, { cache: "no-store" });
      if (!res.ok) return;
      const { json } = await readJsonOrText(res);
      const all = (json ?? []) as Job[];
      const found = Array.isArray(all) ? all.find((j) => j.id === currentJobId) : null;
      if (!found) return;

      setJob(found);
      const created = new Date(found.createdAt).getTime();
      const now = Date.now();
      setElapsedMs(now - created);

      const completed = (Array.isArray(all) ? all : [])
        .filter((j) => j.type === found.type && j.status === "succeeded")
        .slice(0, 12);
      const durations = completed
        .map((j) => new Date(j.updatedAt).getTime() - new Date(j.createdAt).getTime())
        .filter((d) => Number.isFinite(d) && d > 3_000 && d < 6 * 60 * 60 * 1000);

      if (durations.length >= 2) {
        const avg = durations.reduce((a, b) => a + b, 0) / durations.length;
        const remaining = Math.max(8_000, avg - (now - created));
        setEtaMs(remaining);
      } else {
        setEtaMs(null);
      }

      if (found.status === "succeeded" || found.status === "failed") {
        if (typeof window !== "undefined") {
          window.localStorage.removeItem("inventory-intelligence:currentJobId");
        }
        setCurrentJobId(null);
        setCurrentJobType(null);
      }
    }

    void refreshJob();
    poll = setInterval(() => void refreshJob(), 3000);
    tick = setInterval(() => {
      if (cancelled) return;
      setElapsedMs((v) => v + 1000);
      setEtaMs((v) => (v == null ? null : Math.max(0, v - 1000)));
    }, 1000);

    return () => {
      cancelled = true;
      if (poll) clearInterval(poll);
      if (tick) clearInterval(tick);
    };
  }, [currentJobId]);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setMessage(null);
    setBusy(true);
    try {
      const form = new FormData(e.currentTarget);

      if (mode === "upload") {
        if (stagedFiles.length === 0) {
          throw new Error("Add files or folders using the box below.");
        }
        const fd = new FormData();
        const maxFilesVal = String(form.get("maxFiles") ?? "");
        if (maxFilesVal.trim() !== "") fd.append("maxFiles", maxFilesVal);
        appendUploadFiles(fd, stagedFiles);
        const res = await fetch(`${getApiBase()}/api/jobs/upload`, {
          method: "POST",
          body: fd,
        });
        const data = await parseJsonOrThrow<{ jobId: string }>(res, "Upload");
        if (typeof window !== "undefined") {
          window.localStorage.setItem("inventory-intelligence:currentJobId", String(data.jobId));
        }
        setCurrentJobId(String(data.jobId));
        setCurrentJobType("build_inventory_from_upload");
        notifyJobsTableActive();
        setMessage(`Upload started (job ${String(data.jobId).slice(0, 8)}...).`);
        setStagedFiles([]);
        return;
      }

      if (mode === "dump") {
        const dumpFolder = String(form.get("dumpFolder") || "");
        const maxFilesRaw = String(form.get("maxFiles") || "");
        const maxFiles = maxFilesRaw.trim() === "" ? null : Number(maxFilesRaw);
        const res = await fetch(`${getApiBase()}/api/jobs/build`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ dumpFolder, maxFiles }),
        });
        const data = await parseJsonOrThrow<{ jobId: string }>(res, "Build");
        if (typeof window !== "undefined") {
          window.localStorage.setItem("inventory-intelligence:currentJobId", String(data.jobId));
        }
        setCurrentJobId(String(data.jobId));
        setCurrentJobType("build_inventory_from_dump");
        notifyJobsTableActive();
        setMessage(`Job started: ${data.jobId}`);
        return;
      }

      if (mode === "repo") {
        const provider = String(form.get("provider") || "github");
        const repoUrl = String(form.get("repoUrl") || "");
        const ref = String(form.get("ref") || "main");
        const token = String(form.get("token") || "");
        const maxFilesRaw = String(form.get("maxFiles") || "");
        const maxFiles = maxFilesRaw.trim() === "" ? null : Number(maxFilesRaw);
        const res = await fetch(`${getApiBase()}/api/jobs/repo`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            provider,
            repoUrl,
            ref,
            maxFiles,
            token: token.trim() ? token : null,
          }),
        });
        const data = await parseJsonOrThrow<{ jobId: string }>(res, "Repo");
        if (typeof window !== "undefined") {
          window.localStorage.setItem("inventory-intelligence:currentJobId", String(data.jobId));
        }
        setCurrentJobId(String(data.jobId));
        setCurrentJobType("build_inventory_from_repo");
        notifyJobsTableActive();
        setMessage(`Job started: ${data.jobId}`);
        return;
      }

      if (mode === "segment") {
        const inputPath = String(form.get("inputPath") || "");
        const outputPath = String(form.get("outputPath") || "");
        const res = await fetch(`${getApiBase()}/api/jobs/segment`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ inputPath, outputPath }),
        });
        const data = await parseJsonOrThrow<{ jobId: string }>(res, "Segment");
        if (typeof window !== "undefined") {
          window.localStorage.setItem("inventory-intelligence:currentJobId", String(data.jobId));
        }
        setCurrentJobId(String(data.jobId));
        setCurrentJobType("segment_inventory");
        notifyJobsTableActive();
        setMessage(`Job started: ${data.jobId}`);
        return;
      }
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={Card}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">Run</h2>
          <p className={`mt-1 ${Subtle}`}>{description}</p>
        </div>

        <div className="w-full sm:w-72">
          <label className="grid gap-1">
            <span className={Label}>Mode</span>
            <select
              className={Select}
              value={mode}
              onChange={(e) => {
                setMode(e.target.value as Mode);
                setStagedFiles([]);
              }}
            >
              <option value="dump">Folder path</option>
              <option value="upload">Upload files</option>
              <option value="repo">Repository (GitHub/GitLab)</option>
              <option value="segment">Spreadsheet segmentation</option>
            </select>
          </label>
        </div>
      </div>

      <div className="mt-5 text-sm font-medium text-zinc-900 dark:text-zinc-50">{title}</div>

      {currentJobId && (job?.status === "running" || job?.status === "queued" || !job) ? (
        <div className="mt-4 rounded-2xl border border-zinc-200/70 dark:border-zinc-800/70 bg-zinc-50/60 dark:bg-zinc-900/20 p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className="h-2 w-2 rounded-full bg-blue-600 dark:bg-blue-400 animate-pulse" />
                <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">
                  Working on your run
                </div>
              </div>
              <div className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                Job <span className="font-mono">{currentJobId.slice(0, 8)}...</span>{" "}
                {currentJobType ? (
                  <span className="text-zinc-500">- {currentJobType.replaceAll("_", " ")}</span>
                ) : null}
              </div>
            </div>
            {job?.status ? (
              <span className="text-xs text-zinc-500 dark:text-zinc-400">{job.status}</span>
            ) : null}
          </div>

          <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-zinc-200/70 dark:bg-zinc-800/70">
            <div className="h-full w-[42%] animate-[loader_1.2s_ease-in-out_infinite] rounded-full bg-linear-to-r from-blue-500/30 via-blue-500 to-blue-500/30" />
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-zinc-600 dark:text-zinc-400">
            <div>
              <span className="text-zinc-500 dark:text-zinc-500">Elapsed:</span> {fmtDuration(elapsedMs)}
            </div>
            <div>
              <span className="text-zinc-500 dark:text-zinc-500">ETA:</span>{" "}
              {etaMs == null ? "calculating..." : `~${fmtDuration(etaMs)}`}
            </div>
          </div>
        </div>
      ) : null}

      <form className="mt-4 grid gap-3" onSubmit={onSubmit}>
        {mode === "dump" ? (
          <>
            <label className="grid gap-1">
              <span className={Label}>Folder path (name or relative path)</span>
              <input
                name="dumpFolder"
                className={Input}
                placeholder="May 23 - PacketAI DD"
                required
              />
            </label>
            <label className="grid gap-1">
              <span className={Label}>Max files (optional)</span>
              <input name="maxFiles" className={Input} placeholder="e.g. 500" />
            </label>
            <div className="text-[12px] text-zinc-500 dark:text-zinc-500">
              Output will be saved to your Downloads folder automatically.
            </div>
          </>
        ) : null}

        {mode === "upload" ? (
          <>
            <input
              ref={filePickerRef}
              type="file"
              multiple
              className="sr-only"
              aria-label="Choose files to upload"
              onChange={(e) => {
                mergeIncoming(e.target.files);
                e.target.value = "";
              }}
            />
            <input
              ref={folderPickerRef}
              type="file"
              multiple
              className="sr-only"
              aria-label="Choose a folder to upload (includes subfolders)"
              {...({
                webkitdirectory: "",
                mozdirectory: "",
                directory: "",
              } as InputHTMLAttributes<HTMLInputElement>)}
              onChange={(e) => {
                mergeIncoming(e.target.files);
                e.target.value = "";
              }}
            />
            <div className="grid gap-2">
              <span className={Label}>Files and folders</span>
              <div
                onDragEnter={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setDragActive(true);
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setDragActive(true);
                }}
                onDragLeave={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setDragActive(false);
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setDragActive(false);
                  mergeIncoming(e.dataTransfer.files);
                }}
                className={[
                  "flex min-h-56 flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed px-4 py-8 text-center transition-colors",
                  dragActive
                    ? "border-blue-500 bg-blue-50/80 dark:border-blue-400 dark:bg-blue-950/40"
                    : "border-blue-200 bg-blue-50/40 hover:border-blue-300 dark:border-blue-900 dark:bg-blue-950/20 dark:hover:border-blue-700",
                ].join(" ")}
              >
                <div className="text-sm font-medium text-zinc-800 dark:text-zinc-100">
                  Drop files here, or use the buttons below
                </div>
                <div className="text-[12px] text-zinc-600 dark:text-zinc-400 max-w-md">
                  Browsers cannot select multiple folders in one system dialog. Use{" "}
                  <span className="font-medium text-zinc-800 dark:text-zinc-200">Add folder</span> once
                  per folder (subfolders are included). You can mix files and several folder picks.
                </div>
                <div
                  className="flex flex-wrap items-center justify-center gap-2"
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    type="button"
                    className="h-9 rounded-xl bg-blue-600 px-4 text-sm font-semibold text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-400"
                    onClick={() => filePickerRef.current?.click()}
                  >
                    Add files
                  </button>
                  <button
                    type="button"
                    className="h-9 rounded-xl border border-blue-300 bg-white px-4 text-sm font-semibold text-blue-900 hover:bg-blue-50 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-100 dark:hover:bg-blue-900/60"
                    onClick={() => folderPickerRef.current?.click()}
                  >
                    Add folder
                  </button>
                  <button
                    type="button"
                    className="h-9 rounded-xl border border-zinc-200 px-4 text-sm font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
                    onClick={() => setStagedFiles([])}
                    disabled={stagedFiles.length === 0}
                  >
                    Clear
                  </button>
                </div>
                <div className="text-xs text-zinc-500 dark:text-zinc-500">
                  Selected:{" "}
                  <span className="font-semibold text-zinc-700 dark:text-zinc-300">
                    {stagedFiles.length}
                  </span>{" "}
                  file{stagedFiles.length === 1 ? "" : "s"}
                </div>
              </div>
            </div>
            <label className="grid gap-1">
              <span className={Label}>Max files (optional)</span>
              <input name="maxFiles" className={Input} placeholder="e.g. 500" />
            </label>
          </>
        ) : null}

        {mode === "repo" ? (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="grid gap-1">
                <span className={Label}>Provider</span>
                <select name="provider" className={Select} defaultValue="github">
                  <option value="github">GitHub</option>
                  <option value="gitlab">GitLab</option>
                </select>
              </label>
              <label className="grid gap-1">
                <span className={Label}>Ref</span>
                <input name="ref" className={Input} defaultValue="main" />
                <span className="text-[12px] text-zinc-500 dark:text-zinc-500">Branch, tag, or SHA.</span>
              </label>
            </div>
            <label className="grid gap-1">
              <span className={Label}>Repo URL</span>
              <input name="repoUrl" className={Input} placeholder="https://github.com/org/repo" required />
            </label>
            <label className="grid gap-1">
              <span className={Label}>Token (optional; not stored)</span>
              <input name="token" type="password" className={Input} placeholder="ghp_... / glpat-..." />
            </label>
            <label className="grid gap-1">
              <span className={Label}>Max files (optional)</span>
              <input name="maxFiles" className={Input} placeholder="e.g. 1000" />
            </label>
            <div className="text-[12px] text-zinc-500 dark:text-zinc-500">
              Output will be saved to your Downloads folder automatically.
            </div>
          </>
        ) : null}

        {mode === "segment" ? (
          <>
            <label className="grid gap-1">
              <span className={Label}>Input (.xlsx/.csv)</span>
              <input name="inputPath" className={Input} placeholder="/path/to/inventory.xlsx" required />
            </label>
            <label className="grid gap-1">
              <span className={Label}>Output (.xlsx)</span>
              <input name="outputPath" className={Input} placeholder="/path/to/segmented.xlsx" required />
            </label>
          </>
        ) : null}

        <div className="mt-1 flex items-center gap-3">
          <button className={`${Primary} px-4`} disabled={busy}>
            {busy ? "Working..." : "Start"}
          </button>
          <div className="text-sm text-zinc-500 dark:text-zinc-400">{message}</div>
        </div>
      </form>
    </section>
  );
}

