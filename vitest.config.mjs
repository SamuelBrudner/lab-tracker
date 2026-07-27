import { defineConfig } from "vitest/config";

const frontendTestDefaults = {
  environment: "jsdom",
  globals: true,
  setupFiles: ["src/lab_tracker/frontend_src/test/setup.js"],
  // App.test.jsx drives full-App journeys (several mocked request/render
  // cycles) under the "unit" project. Vitest's default 5s per-test budget is
  // ample locally but intermittently too tight on a loaded two-core CI runner,
  // producing spurious timeouts / "expected null" assertions. Give every
  // frontend test real headroom; genuinely broken UI still fails, just later.
  testTimeout: 15_000,
};

const allFrontendTests = "src/lab_tracker/frontend_src/**/*.test.{js,jsx}";
const featureIntegrationTests =
  "src/lab_tracker/frontend_src/**/*.integration.test.{js,jsx}";

export default defineConfig({
  test: {
    projects: [
      {
        test: {
          ...frontendTestDefaults,
          name: "unit",
          include: [allFrontendTests],
          exclude: [featureIntegrationTests],
        },
      },
      {
        test: {
          ...frontendTestDefaults,
          name: "feature-integration",
          include: [featureIntegrationTests],
        },
      },
    ],
  },
});
