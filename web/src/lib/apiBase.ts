import { normalizePublicPrefix, PUBLIC_URL_PREFIX } from "@/lib/deployBasePath";

const HTML_ATTR = "data-inventory-base-path";

/**
 * Prefix for same-origin API and asset paths from the browser (and SSR links).
 * Order: DOM (SSR), compiled production prefix, then `NEXT_PUBLIC_BASE_PATH` from env.
 */
export function getApiBase(): string {
  if (typeof document !== "undefined") {
    const raw = document.documentElement.getAttribute(HTML_ATTR);
    if (raw !== null && raw !== "") {
      return normalizePublicPrefix(raw);
    }
  }
  const compiled = normalizePublicPrefix(PUBLIC_URL_PREFIX);
  if (compiled !== "") {
    return compiled;
  }
  return normalizePublicPrefix(process.env.NEXT_PUBLIC_BASE_PATH ?? "");
}

export { HTML_ATTR as INVENTORY_BASE_PATH_ATTR };
