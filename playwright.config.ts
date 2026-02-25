import { defineConfig } from "@playwright/test";

const localNoProxy = "127.0.0.1,localhost";
const currentNoProxy = process.env.NO_PROXY || process.env.no_proxy || "";
if (!currentNoProxy.includes("127.0.0.1")) {
  const next = currentNoProxy ? `${currentNoProxy},${localNoProxy}` : localNoProxy;
  process.env.NO_PROXY = next;
  process.env.no_proxy = next;
}

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: "http://127.0.0.1:4173",
    viewport: { width: 1280, height: 720 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: "npm run e2e:server",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
