import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { UpdateAvailableBanner } from "./ui.jsx";

describe("UpdateAvailableBanner", () => {
  it("prompts for a deliberate reload when an update is ready", () => {
    const onReload = vi.fn();

    render(<UpdateAvailableBanner onReload={onReload} />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "An updated version of Lab Tracker is ready."
    );
    fireEvent.click(screen.getByRole("button", { name: "Reload to update" }));

    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("stays hidden when no update is ready", () => {
    const { container } = render(<UpdateAvailableBanner />);

    expect(container).toBeEmptyDOMElement();
  });
});
