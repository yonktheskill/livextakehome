import type { NextConfig } from "next";
const config: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${(process.env.BACKEND_URL || "http://localhost:8000").replace(/\/$/, "")}/api/:path*`,
      },
    ];
  },
};
export default config;
