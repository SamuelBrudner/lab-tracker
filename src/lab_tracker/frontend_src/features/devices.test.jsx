import { describe, expect, it } from "vitest";

import { setupCommands } from "./devices.jsx";

describe("registered computer setup commands", () => {
  it("prompt for the token and include project, capture host, and context", () => {
    const commands = setupCommands({
      capture_context_id: "context-1",
      capture_host_label: "Rig 2 desktop",
      project_id: "project-1",
      secret: "ldev_one-time-secret",
    });

    expect(commands.map((item) => item.label)).toEqual(["PowerShell", "macOS", "Linux"]);
    for (const item of commands) {
      expect(item.command).toContain("lt setup connect");
      expect(item.command).toContain("--save-token");
      expect(item.command).toContain("--prompt-token");
      expect(item.command).not.toContain("ldev_one-time-secret");
      expect(item.command).toContain("--project");
      expect(item.command).toContain("project-1");
      expect(item.command).toContain("--capture-host");
      expect(item.command).toContain("Rig 2 desktop");
      expect(item.command).toContain("--capture-context");
      expect(item.command).toContain("context-1");
      expect(item.command).toContain("--yes");
    }
  });
});
