import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone only when explicitly requested (Docker/Render). Netlify needs default output.
  ...(process.env.OUTPUT_STANDALONE === "1" ? { output: "standalone" as const } : {}),
  devIndicators: false,

};

export default nextConfig;
