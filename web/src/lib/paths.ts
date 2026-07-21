import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

export function downloadsOutputDir() {
  return path.join(os.homedir(), "Downloads", "Inventory Segmenter");
}

export async function ensureDownloadsDir() {
  await fs.mkdir(downloadsOutputDir(), { recursive: true });
}

export function autoOutputPath(jobId: string, hint: string) {
  const safeHint = hint
    .trim()
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "-")
    .replaceAll(/^-+|-+$/g, "")
    .slice(0, 40);
  const name = safeHint ? `${safeHint}-${jobId}.xlsx` : `inventory-${jobId}.xlsx`;
  return path.join(downloadsOutputDir(), name);
}

