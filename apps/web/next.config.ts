import type { NextConfig } from "next";

const API_URL = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  devIndicators: false,
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${API_URL}/:path*`,
      },
    ];
  },
};

export default nextConfig;
