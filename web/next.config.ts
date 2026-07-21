import type { NextConfig } from "next";
import { PUBLIC_URL_PREFIX } from "./src/lib/deployBasePath";

const nextConfig: NextConfig = {
  basePath: PUBLIC_URL_PREFIX,
  env: {
    NEXT_PUBLIC_BASE_PATH: PUBLIC_URL_PREFIX,
  },
  // Jobs table polls /api/jobs every few seconds; skip dev spam in the terminal.
  logging: {
    incomingRequests: {
      ignore: [/\/api\/jobs/],
    },
  },
};

export default nextConfig;
