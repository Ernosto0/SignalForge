import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Browser calls go to /api/* on the Next.js origin and are proxied to FastAPI (no CORS needed).
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
};

export default nextConfig;
