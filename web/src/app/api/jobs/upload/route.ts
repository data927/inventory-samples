import { NextResponse } from "next/server";
import path from "node:path";
import { promises as fs } from "node:fs";
import { createWriteStream } from "node:fs";
import { Readable } from "node:stream";
import Busboy from "busboy";
import { createJob, newJobId, updateJob } from "@/lib/jobs";
import { enqueuePythonJob } from "@/lib/pythonRunner";
import { autoOutputPath, ensureDownloadsDir } from "@/lib/paths";

/** Strip traversal; keep nested paths from folder uploads (webkitRelativePath). */
function safeRelPath(raw: string) {
  let s = raw.replaceAll("\\", "/").trim();
  if (s.startsWith("/")) s = s.slice(1);
  const parts = s.split("/").filter((p) => p && p !== ".." && p !== ".");
  return parts.join("/");
}

export async function POST(req: Request) {
  // Streaming multipart upload so large uploads don't buffer in memory.
  const MAX_FILES = Number(process.env.UPLOAD_MAX_FILES || "2000");
  const MAX_TOTAL_BYTES = Number(process.env.UPLOAD_MAX_TOTAL_BYTES || String(10 * 1024 * 1024 * 1024)); // 10GB
  const MAX_FILE_BYTES = Number(process.env.UPLOAD_MAX_FILE_BYTES || String(750 * 1024 * 1024)); // 750MB

  const contentType = req.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("multipart/form-data")) {
    return NextResponse.json({ error: "Expected multipart/form-data upload" }, { status: 400 });
  }
  if (!req.body) {
    return NextResponse.json({ error: "Missing request body" }, { status: 400 });
  }

  const job = await createJob({
    id: newJobId(),
    type: "build_inventory_from_upload",
    status: "queued",
    input: {
      uploadedFiles: 0,
      maxFiles: null,
    },
  });

  const uploadsRoot = path.join(process.cwd(), ".data", "uploads", job.id);
  await ensureDownloadsDir();
  const outPath = autoOutputPath(job.id, "upload");
  await fs.mkdir(uploadsRoot, { recursive: true });

  let maxFiles: number | null = null;
  let uploadedFiles = 0;
  let totalBytes = 0;
  const seen = new Set<string>();
  /** Busboy "finish" runs when parsing ends, not when disk writes finish — await these too. */
  const fileWrites: Promise<void>[] = [];
  let acceptedFileParts = 0;

  const busboy = Busboy({
    headers: Object.fromEntries(req.headers.entries()),
    limits: {
      files: MAX_FILES,
      fileSize: MAX_FILE_BYTES,
    },
  });

  const done = new Promise<void>((resolve, reject) => {
    busboy.on("field", (name, value) => {
      if (name === "maxFiles") {
        const v = String(value || "").trim();
        if (v !== "") {
          const n = Number(v);
          if (Number.isFinite(n) && n > 0) maxFiles = n;
        }
      }
    });

    busboy.on("file", (fieldname, file, info) => {
      if (fieldname !== "files") {
        file.resume();
        return;
      }
      if (acceptedFileParts >= MAX_FILES) {
        file.resume();
        reject(new Error(`Too many files (max ${MAX_FILES}).`));
        return;
      }
      acceptedFileParts += 1;

      let rel = safeRelPath(info.filename || "file");
      if (!rel) rel = `upload-${seen.size}`;
      let destRel = rel;
      let i = 2;
      while (seen.has(destRel.toLowerCase())) {
        const ext = path.extname(rel);
        const stem = rel.slice(0, rel.length - ext.length);
        destRel = `${stem}-${i}${ext}`;
        i += 1;
      }
      seen.add(destRel.toLowerCase());

      const dest = path.join(uploadsRoot, destRel);
      const writeDone = fs.mkdir(path.dirname(dest), { recursive: true }).then(
        () =>
          new Promise<void>((resolveWrite, rejectWrite) => {
            const out = createWriteStream(dest);
            file.on("data", (chunk: Buffer) => {
              totalBytes += chunk.length;
              if (totalBytes > MAX_TOTAL_BYTES) {
                file.unpipe(out);
                out.destroy();
                rejectWrite(
                  new Error(`Upload too large (max ${(MAX_TOTAL_BYTES / (1024 * 1024 * 1024)).toFixed(0)}GB).`),
                );
              }
            });
            out.on("error", rejectWrite);
            file.on("error", rejectWrite);
            file.on("limit", () =>
              rejectWrite(new Error(`File too large (max ${(MAX_FILE_BYTES / (1024 * 1024)).toFixed(0)}MB).`)),
            );
            out.on("finish", () => {
              uploadedFiles += 1;
              void updateJob(job.id, {
                input: { uploadedFiles, maxFiles },
              });
              resolveWrite();
            });
            file.pipe(out);
          }),
      );
      fileWrites.push(writeDone);
    });

    busboy.on("filesLimit", () => reject(new Error(`Too many files (max ${MAX_FILES}).`)));
    busboy.on("error", (err) => reject(err));
    busboy.on("finish", () => {
      void Promise.all(fileWrites).then(() => resolve(), reject);
    });
  });

  try {
    Readable.fromWeb(req.body as any).pipe(busboy);
    await done;
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Upload failed";
    await updateJob(job.id, { status: "failed", error: msg, outputPath: outPath });
    return NextResponse.json({ error: msg }, { status: 400 });
  }

  if (uploadedFiles === 0) {
    await updateJob(job.id, { status: "failed", error: "No files uploaded", outputPath: outPath });
    return NextResponse.json({ error: "No files uploaded" }, { status: 400 });
  }

  await updateJob(job.id, { input: { uploadedFiles, maxFiles } });

  const pythonCode =
    "from server import build_inventory_from_dump; " +
    `print(build_inventory_from_dump(dump_path=${JSON.stringify(uploadsRoot)}, output_path=${JSON.stringify(outPath)}, max_files=${maxFiles ?? "None"}))`;

  // Enqueue processing to avoid overloading the machine on heavy uploads.
  await enqueuePythonJob({
    jobId: job.id,
    pythonCode,
    outputPath: outPath,
  });

  const base = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/$/, "");
  return NextResponse.json({
    jobId: job.id,
    outputPath: outPath,
    downloadUrl: `${base}/api/jobs/${job.id}/download`,
  });
}
