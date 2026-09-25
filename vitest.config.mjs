import { defineConfig } from "vitest/config";

const frontendTestDefaults = {
  environment: "jsdom",
  globals: true,
  // App.test.jsx drives full-App journeys (several mocked request/render
  // cycles) under the "unit" project. Vitest's default 5s per-test budget is
  // ample locally but intermittently too tight on a loaded two-core CI runner,
  // producing spurious timeouts / "expected null" assertions. Give every
  // frontend test real headroom; genuinely broken UI still fails, just later.
  testTimeout: 15_000,
};

const sharedSetup = "src/lab_tracker/frontend_src/test/setup.js";
// `vitest run --mode scheduler-chaos` (npm run test:frontend:chaos) runs
// React's effects after Testing Library's setTimeout(0) drain, so a test that
// races them fails every time. The harness must run before the shared setup
// imports react-dom; see test/scheduler-chaos.js.
const schedulerChaosMode = "scheduler-chaos";
const schedulerChaosSetup = "src/lab_tracker/frontend_src/test/scheduler-chaos.js";

const allFrontendTests = "src/lab_tracker/frontend_src/**/*.test.{js,jsx}";
const featureIntegrationTests =
  "src/lab_tracker/frontend_src/**/*.integration.test.{js,jsx}";

export default defineConfig(({ mode }) => {
  const setupFiles =
    mode === schedulerChaosMode ? [schedulerChaosSetup, sharedSetup] : [sharedSetup];
  return {
    test: {
      projects: [
        {
          test: {
            ...frontendTestDefaults,
            setupFiles,
            name: "unit",
            include: [allFrontendTests],
            exclude: [featureIntegrationTests],
          },
        },
        {
          test: {
            ...frontendTestDefaults,
            setupFiles,
            name: "feature-integration",
            include: [featureIntegrationTests],
          },
        },
      ],
    },
  };
});
