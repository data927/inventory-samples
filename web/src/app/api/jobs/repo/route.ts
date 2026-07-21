import { NextResponse } from "next/server";
import { createJob, newJobId, updateJob } from "@/lib/jobs";
import { enqueuePythonJob } from "@/lib/pythonRunner";
import { autoOutputPath, ensureDownloadsDir } from "@/lib/paths";

export async function POST(req: Request) {
  const body = (await req.json()) as {
    provider: "github" | "gitlab";
    repoUrl: string;
    ref?: string;
    maxFiles?: number | null;
    token?: string | null;
  };

  if (!body?.provider || !body?.repoUrl) {
    return NextResponse.json(
      { error: "provider and repoUrl are required" },
      { status: 400 },
    );
  }

  await ensureDownloadsDir();
  // IMPORTANT: do NOT store token anywhere (jobs DB/logs).
  const job = await createJob({
    id: newJobId(),
    type: "build_inventory_from_repo",
    status: "queued",
    input: {
      provider: body.provider,
      repoUrl: body.repoUrl,
      ref: body.ref ?? "main",
      maxFiles: body.maxFiles ?? null,
      // token intentionally omitted
    },
  });

  const outPath = autoOutputPath(job.id, "repo");
  const pythonCode =
    "from server import build_inventory_from_repo; " +
    `print(build_inventory_from_repo(provider=${JSON.stringify(body.provider)}, repo_url=${JSON.stringify(body.repoUrl)}, ref=${JSON.stringify(body.ref ?? "main")}, output_path=${JSON.stringify(outPath)}, max_files=${body.maxFiles ?? "None"}))`;

  const env: Record<string, string | undefined> = {};
  if (body.provider === "github" && body.token) env.GITHUB_TOKEN = body.token;
  if (body.provider === "gitlab" && body.token) env.GITLAB_TOKEN = body.token;

  await enqueuePythonJob({
    jobId: job.id,
    pythonCode,
    env,
    outputPath: outPath,
  });

  return NextResponse.json({ jobId: job.id });
}

