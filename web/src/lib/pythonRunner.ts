import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { promises as fs } from "node:fs";
import path from "node:path";
import { getJobQueue } from "@/lib/jobQueue";
import { listJobs, updateJob } from "@/lib/jobs";

export type SpawnResult = {
  pid: number;
  logPath: string;
};

function repoRootFromWebCwd() {
  // web/ is a subfolder inside the repo
  return path.resolve(process.cwd(), "..");
}

/** Absolute path to the Python binary. Relative `PYTHON_BIN` in `.env` is resolved from `web/` (not from `cwd` passed to `spawn`, which is repo root). */
function resolvePythonBin(repoRoot: string): string {
  const webDir = path.join(repoRoot, "web");
  const venvPython = path.join(repoRoot, ".venv", "bin", "python");
  const raw = process.env.PYTHON_BIN?.trim();

  if (raw) {
    const candidate = path.isAbsolute(raw) ? raw : path.resolve(webDir, raw);
    if (existsSync(candidate)) {
      return candidate;
    }
    const fromRepo = path.resolve(repoRoot, raw);
    if (existsSync(fromRepo)) {
      return fromRepo;
    }
    return candidate;
  }

  if (existsSync(venvPython)) {
    return venvPython;
  }

  return "python3";
}

export async function spawnPythonJob(opts: {
  jobId: string;
  pythonCode: string;
  env?: Record<string, string | undefined>;
}): Promise<SpawnResult> {
  const repoRoot = repoRootFromWebCwd();
  const logsDir = path.join(process.cwd(), ".data", "logs");
  await fs.mkdir(logsDir, { recursive: true });
  const logPath = path.join(logsDir, `${opts.jobId}.log.txt`);

  const pythonBin = resolvePythonBin(repoRoot);
  const child = spawn(pythonBin, ["-c", opts.pythonCode], {
    cwd: repoRoot,
    env: {
      ...process.env,
      ...opts.env,
      // Ensure python can import repo-root modules like server.py
      PYTHONPATH: repoRoot,
    },
  });

  const logStream = await fs.open(logPath, "a");
  child.stdout.on("data", async (d) => {
    await logStream.appendFile(d);
  });
  child.stderr.on("data", async (d) => {
    await logStream.appendFile(d);
  });
  child.on("close", async () => {
    await logStream.close();
  });

  return { pid: child.pid ?? -1, logPath };
}

async function mergeJobInput(jobId: string, patch: Record<string, unknown>) {
  const jobs = await listJobs();
  const cur = jobs.find((j) => j.id === jobId);
  const next = { ...(cur?.input ?? {}), ...patch };
  await updateJob(jobId, { input: next });
}

export async function enqueuePythonJob(opts: {
  jobId: string;
  pythonCode: string;
  outputPath?: string;
  env?: Record<string, string | undefined>;
}) {
  const queue = getJobQueue();
  const { position, promise } = queue.enqueue(opts.jobId, async () => {
    await updateJob(opts.jobId, { status: "running" });
    await mergeJobInput(opts.jobId, { phase: "running" });
    const { pid, logPath } = await spawnPythonJob({
      jobId: opts.jobId,
      pythonCode: opts.pythonCode,
      env: opts.env,
    });
    await updateJob(opts.jobId, {
      status: "running",
      pid,
      logPath,
      outputPath: opts.outputPath,
    });
    await mergeJobInput(opts.jobId, { phase: "running" });
    return { pid, logPath };
  });

  await updateJob(opts.jobId, {
    status: "queued",
    outputPath: opts.outputPath,
  });
  await mergeJobInput(opts.jobId, { phase: "queued", queuePosition: position });

  // Fire-and-forget; route handlers return immediately.
  void promise.catch(async (e) => {
    const msg = e instanceof Error ? e.message : "Failed to start job";
    await updateJob(opts.jobId, { status: "failed", error: msg, outputPath: opts.outputPath });
    await mergeJobInput(opts.jobId, { phase: "failed" });
  });

  return { position };
}

