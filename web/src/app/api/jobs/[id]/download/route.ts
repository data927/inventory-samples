import { NextResponse } from "next/server";
import path from "node:path";
import { promises as fs } from "node:fs";
import { listJobs } from "@/lib/jobs";
import { downloadsOutputDir } from "@/lib/paths";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const jobs = await listJobs();
  const job = jobs.find((j) => j.id === id);
  const outPath = job?.outputPath;
  if (!outPath) {
    return NextResponse.json({ error: "No outputPath for job" }, { status: 404 });
  }

  // Only allow downloads from ~/Downloads/Inventory Segmenter
  const allowedRoot = downloadsOutputDir() + path.sep;
  const resolved = path.resolve(outPath);
  if (!resolved.startsWith(allowedRoot)) {
    return NextResponse.json({ error: "Output not downloadable" }, { status: 403 });
  }

  try {
    const buf = await fs.readFile(resolved);
    return new NextResponse(buf, {
      status: 200,
      headers: {
        "content-type":
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "content-disposition": `attachment; filename="inventory-${id}.xlsx"`,
      },
    });
  } catch {
    return NextResponse.json({ error: "Output not found" }, { status: 404 });
  }
}

