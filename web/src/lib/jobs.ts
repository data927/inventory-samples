import { promises as fs } from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

export type JobType =
  | "build_inventory_from_dump"
  | "build_inventory_from_upload"
  | "build_inventory_from_repo"
  | "segment_inventory";
export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export type JobRecord = {
  id: string;
  type: JobType;
  status: JobStatus;
  createdAt: string;
  updatedAt: string;
  input?: Record<string, unknown>;
  outputPath?: string;
  error?: string;
  logPath?: string;
  pid?: number;
};

const DATA_DIR = path.join(process.cwd(), ".data");
const JOBS_PATH = path.join(DATA_DIR, "jobs.json");

declare global {
  // eslint-disable-next-line no-var
  var __inventoryJobsLock: Promise<void> | undefined;
}

function getJobsLock() {
  if (!globalThis.__inventoryJobsLock) globalThis.__inventoryJobsLock = Promise.resolve();
  return globalThis.__inventoryJobsLock;
}

async function withJobsLock<T>(fn: () => Promise<T>): Promise<T> {
  let release: (() => void) | undefined;
  const prev = getJobsLock();
  const next = new Promise<void>((res) => {
    release = res;
  });
  globalThis.__inventoryJobsLock = prev.then(() => next);
  await prev;
  try {
    return await fn();
  } finally {
    if (release) release();
  }
}

async function ensureDataDir() {
  await fs.mkdir(DATA_DIR, { recursive: true });
}

export async function listJobs(): Promise<JobRecord[]> {
  await ensureDataDir();
  try {
    const raw = await fs.readFile(JOBS_PATH, "utf-8");
    const jobs = JSON.parse(raw) as JobRecord[];
    return Array.isArray(jobs) ? jobs : [];
  } catch {
    return [];
  }
}

async function writeJobs(jobs: JobRecord[]) {
  await ensureDataDir();
  await fs.writeFile(JOBS_PATH, JSON.stringify(jobs, null, 2), "utf-8");
}

export function newJobId() {
  return crypto.randomUUID();
}

export async function createJob(init: Omit<JobRecord, "createdAt" | "updatedAt">) {
  return await withJobsLock(async () => {
    const now = new Date().toISOString();
    const job: JobRecord = { ...init, createdAt: now, updatedAt: now };
    const jobs = await listJobs();
    jobs.unshift(job);
    await writeJobs(jobs.slice(0, 200));
    return job;
  });
}

export async function updateJob(id: string, patch: Partial<JobRecord>) {
  return await withJobsLock(async () => {
    const jobs = await listJobs();
    const idx = jobs.findIndex((j) => j.id === id);
    if (idx === -1) return null;
    const next: JobRecord = {
      ...jobs[idx],
      ...patch,
      updatedAt: new Date().toISOString(),
    };
    jobs[idx] = next;
    await writeJobs(jobs);
    return next;
  });
}

