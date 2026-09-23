import { describe, expect, it } from "vitest";

import { installStepsForUserAgent } from "./MobileInstallPrompt.jsx";

const IOS_SAFARI =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 " +
  "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";
const ANDROID_CHROME =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/120.0.0.0 Mobile Safari/537.36";

describe("installStepsForUserAgent", () => {
  it("gives Safari share-sheet steps on iPhone and iPad", () => {
    const steps = installStepsForUserAgent(IOS_SAFARI);
    expect(steps[0]).toMatch(/Share button in Safari/);
    expect(steps).toHaveLength(3);
  });

  it("gives browser-menu steps on Android instead of Safari instructions", () => {
    const steps = installStepsForUserAgent(ANDROID_CHROME);
    expect(steps.join(" ")).not.toMatch(/Safari/);
    expect(steps[0]).toMatch(/browser menu/);
    expect(steps[1]).toMatch(/Install app/);
  });

  it("falls back to generic menu steps for an unknown platform", () => {
    const steps = installStepsForUserAgent("SomeBrowser/1.0");
    expect(steps.join(" ")).not.toMatch(/Safari/);
    expect(steps).toHaveLength(3);
  });
});
