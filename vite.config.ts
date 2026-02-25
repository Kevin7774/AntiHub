import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/cases": {
        target: "http://127.0.0.1:8010",
        changeOrigin: true,
      },
      "/error-codes": {
        target: "http://127.0.0.1:8010",
        changeOrigin: true,
      },
      "/case-templates": {
        target: "http://127.0.0.1:8010",
        changeOrigin: true,
      },
      "/ws": {
        target: "http://127.0.0.1:8010",
        ws: true,
        changeOrigin: true,
      },
      "/docs": {
        target: "http://127.0.0.1:8010",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    watch: false,
  },
});
