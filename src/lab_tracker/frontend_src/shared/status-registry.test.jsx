import * as React from "react";

import { readFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { STATUS_REGISTRY, statusPill, TONE_GLYPHS, TONES } from "./status-registry.js";
import { StatusPill } from "./ui.jsx";

const stylesheet = readFileSync(
  resolvePath(process.cwd(), "src/lab_tracker/frontend/styles.css"),
  "utf8"
);

describe("status registry", () => {
  it("maps every declared status onto a known tone", () => {
    for (const [family, values] of Object.entries(STATUS_REGISTRY)) {
      for (const [value, entry] of Object.entries(values)) {
        expect(TONES, `${family}.${value}`).toContain(entry.tone);
        expect(entry.label, `${family}.${value} label`).toBeTruthy();
      }
    }
  });

  it("gives every tone a glyph, so colour is never the only signal", () => {
    for (const tone of TONES) {
      expect(TONE_GLYPHS[tone], tone).toBeTruthy();
    }
  });

  it("degrades an unknown status to a readable neutral pill", () => {
    // A new API status should look plain, not break the page or vanish.
    const pill = statusPill("review", "some_new_state");
    expect(pill.tone).toBe("neutral");
    expect(pill.label).toBe("Some new state");
    expect(pill.className).toBe("pill tone-neutral");
    expect(pill.ariaLabel).toBe("Status: Some new state");
  });

  it("degrades an unknown family too", () => {
    const pill = statusPill("nonexistent", "whatever");
    expect(pill.tone).toBe("neutral");
    expect(pill.ariaLabel).toBe("Nonexistent: Whatever");
  });

  it("names the family in the aria-label", () => {
    // "Rejected" alone tells a screen-reader user nothing about what was.
    expect(statusPill("review", "rejected").ariaLabel).toBe("Status: Rejected");
    expect(statusPill("role", "admin").ariaLabel).toBe("Role: Admin");
    expect(statusPill("session", "scientific").ariaLabel).toBe("Session type: Scientific");
    expect(statusPill("flag", "critical").ariaLabel).toBe("Flag: Critical");
  });

  it("treats accepted, applied and committed as one meaning", () => {
    const tones = ["accepted", "applied", "committed"].map((v) => statusPill("review", v).tone);
    expect(new Set(tones).size).toBe(1);
    expect(tones[0]).toBe("success");
  });
});

describe("StatusPill", () => {
  it("renders glyph, word and an aria-label", () => {
    render(<StatusPill family="review" value="rejected" />);

    const pill = screen.getByLabelText("Status: Rejected");
    // The word alone must identify the status, with the glyph reinforcing it.
    expect(within(pill).getByText("Rejected")).toBeInTheDocument();
    expect(pill.textContent).toContain(TONE_GLYPHS.danger);
    expect(pill).toHaveClass("pill", "tone-danger");
  });

  it("keeps the glyph out of the label's text node", () => {
    // Otherwise the pill reads "✗Rejected" and no text query can match the word.
    render(<StatusPill family="review" value="rejected" />);
    expect(screen.getByText("Rejected").textContent).toBe("Rejected");
  });

  it("hides the glyph from assistive tech, which reads the aria-label", () => {
    const { container } = render(<StatusPill family="role" value="admin" />);
    expect(container.querySelector(".pill-glyph")).toHaveAttribute("aria-hidden", "true");
  });

  it("lets a caller supply its own wording without losing the tone", () => {
    render(
      <StatusPill family="flag" value="critical">
        Missing metadata: 3
      </StatusPill>
    );
    const pill = screen.getByLabelText("Flag: Critical");
    expect(pill).toHaveClass("tone-danger");
    expect(within(pill).getByText("Missing metadata: 3")).toBeInTheDocument();
  });
});

describe("status stylesheet", () => {
  it("declares exactly one rule per tone", () => {
    for (const tone of TONES) {
      expect(stylesheet).toContain(`.pill.tone-${tone} {`);
    }
  });

  it("has retired the per-value pill rules the registry replaced", () => {
    // These were the ~17 rules where the same pastel pairs were repeated.
    for (const dead of [
      ".pill.role-admin",
      ".pill.role-editor",
      ".pill.role-viewer",
      ".pill.session-scientific",
      ".pill.session-operational",
      ".pill.review-pending",
      ".pill.review-approved",
      ".pill.review-rejected",
      ".pill.review-changes_requested",
      // Relevance had no consumer anywhere — retained-v1 defers relevance
      // ranking, so the rules were dead rather than pending a meter.
      ".pill.relevance",
    ]) {
      expect(stylesheet, dead).not.toContain(`${dead} {`);
    }
  });

  it("keeps status emphasis so a pill still reads as a status", () => {
    expect(stylesheet).toMatch(/\.pill\[class\*="tone-"\]\s*\{\s*font-weight:\s*700/);
  });
});
