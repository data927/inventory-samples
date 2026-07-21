import { NextResponse } from "next/server";
import { createJob, newJobId, updateJob } from "@/lib/jobs";
import { enqueuePythonJob } from "@/lib/pythonRunner";

export async function POST(req: Request) {
  const body = (await req.json()) as {
    inputPath: string;
    outputPath: string;
  };

  if (!body?.inputPath || !body?.outputPath) {
    return NextResponse.json({ error: "inputPath and outputPath are required" }, { status: 400 });
  }

  const job = await createJob({
    id: newJobId(),
    type: "segment_inventory",
    status: "queued",
    input: { inputPath: body.inputPath, outputPath: body.outputPath },
    outputPath: body.outputPath,
  });

  const pythonCode =
    "from server import segment_inventory; " +
    `print(segment_inventory(input_path=${JSON.stringify(body.inputPath)}, output_path=${JSON.stringify(body.outputPath)}))`;

  await enqueuePythonJob({
    jobId: job.id,
    pythonCode,
    outputPath: body.outputPath,
  });

  return NextResponse.json({ jobId: job.id });
}

