import { NextResponse } from "next/server";
import { listJobs } from "@/lib/jobs";
import { refreshJobStatus } from "@/lib/jobRuntime";

export async function GET() {
  const jobs = await listJobs();
  const refreshed = await Promise.all(jobs.map((j) => refreshJobStatus(j)));
  return NextResponse.json(refreshed);
}

