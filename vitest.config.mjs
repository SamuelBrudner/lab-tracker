import { defineConfig } from "vitest/config";

const frontendTestDefaults = {
  environment: "jsdom",
  globals: true,
  setupFiles: ["src/lab_tracker/frontend_src/test/setup.js"],
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
          // Full-App journeys perform several mocked request/render cycles. A
          // loaded two-core CI runner needs scheduling headroom, while the
          // unit project deliberately retains Vitest's normal 5s budget.
          testTimeout: 15_000,
        },
      },
    ],
  },
});
