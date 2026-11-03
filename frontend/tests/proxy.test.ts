import { afterEach, describe, expect, it, vi } from "vitest";
import config from "../next.config";
afterEach(() => vi.unstubAllEnvs());
describe("same-origin backend proxy configuration", () => {
  it("preserves the /api path with the local backend default", async () => {
    vi.stubEnv("BACKEND_URL", "");
    expect(await config.rewrites!()).toEqual([
      { source: "/api/:path*", destination: "http://localhost:8000/api/:path*" },
    ]);
  });
  it("supports the internal backend URL and normalizes a trailing slash", async () => {
    vi.stubEnv("BACKEND_URL", "http://backend:8000/");
    expect(await config.rewrites!()).toEqual([
      { source: "/api/:path*", destination: "http://backend:8000/api/:path*" },
    ]);
  });
});
