/**
 * URL prefix for this app behind nginx. Must stay in sync with `basePath` in `next.config.ts`.
 * Do not rely on `.env` alone — that can drift from the real `basePath` and break client fetches.
 */
export const PUBLIC_URL_PREFIX =
  process.env.NODE_ENV === "production" ? "/inventory_segmentor" : "";

export function normalizePublicPrefix(s: string): string {
  return s.replace(/\/$/, "");
}
