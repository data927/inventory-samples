import { promises as fs } from "node:fs";
import { updateJob, type JobRecord } from "@/lib/jobs";

function isPidRunning(pid: number) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function readLogTail(logPath: string, maxBytes = 32_000): Promise<string> {
  try {
    const buf = await fs.readFile(logPath);
    if (buf.byteLength <= maxBytes) return buf.toString("utf-8");
    return buf.subarray(buf.byteLength - maxBytes).toString("utf-8");
  } catch {
    return "";
  }
}

function inferFailureFromLog(log: string) {
  const needles = [
    "Traceback (most recent call last):",
    "Error code:",
    "Exception:",
    "RuntimeError:",
    "IllegalCharacterError",
  ];
  return needles.some((n) => log.includes(n));
}

export async function refreshJobStatus(job: JobRecord): Promise<JobRecord> {
  if (job.status !== "running" || !job.pid) return job;
  if (job.pid <= 0) return job;

  if (isPidRunning(job.pid)) return job;

  const log = job.logPath ? await readLogTail(job.logPath) : "";
  const failed = inferFailureFromLog(log);
  const error = failed ? "Process exited; see logs." : undefined;
  const updated = await updateJob(job.id, {
    status: failed ? "failed" : "succeeded",
    error,
  });
  return updated ?? job;
}

