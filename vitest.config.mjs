import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/lab_tracker/frontend_src/**/*.test.jsx"],
    setupFiles: ["src/lab_tracker/frontend_src/test/setup.js"],
  },
});
