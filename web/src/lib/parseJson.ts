/**
 * Read a fetch Response. If the body is JSON, return it; if it isn't (typical for
 * nginx HTML error pages on 413 / 502 / 504), throw a clear error including the
 * HTTP status and a short body excerpt — never the raw "Unexpected token '<'".
 */
export async function parseJsonOrThrow<T = unknown>(res: Response, label = "Request"): Promise<T> {
  const ct = (res.headers.get("content-type") || "").toLowerCase();
  const text = await res.text();

  if (!ct.includes("application/json")) {
    const snippet = text.replace(/\s+/g, " ").trim().slice(0, 200);
    if (!res.ok) {
      throw new Error(`${label} failed (HTTP ${res.status})${snippet ? `: ${snippet}` : ""}`);
    }
    throw new Error(`${label}: server returned ${ct || "no content-type"} instead of JSON${snippet ? ` — ${snippet}` : ""}`);
  }

  let data: unknown;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`${label}: invalid JSON body (HTTP ${res.status})`);
  }

  if (!res.ok) {
    const errMsg =
      (data && typeof data === "object" && "error" in (data as Record<string, unknown>) && String((data as Record<string, unknown>).error)) ||
      `HTTP ${res.status}`;
    throw new Error(`${label} failed: ${errMsg}`);
  }
  return data as T;
}
