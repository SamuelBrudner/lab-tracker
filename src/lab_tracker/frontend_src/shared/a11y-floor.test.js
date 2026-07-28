import { readFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

import { describe, expect, it } from "vitest";

const rawStylesheet = readFileSync(
  resolvePath(process.cwd(), "src/lab_tracker/frontend/styles.css"),
  "utf8"
);
// Comments explain these rules and quote the very patterns under test, so strip
// them before matching or a comment can pass or fail an assertion on its own.
const stylesheet = rawStylesheet.replace(/\/\*[\s\S]*?\*\//g, "");

/** Body of the first rule whose selector matches, braces excluded. */
function ruleBody(selector) {
  const index = stylesheet.indexOf(selector);
  expect(index, `rule ${selector}`).toBeGreaterThan(-1);
  const open = stylesheet.indexOf("{", index);
  return stylesheet.slice(open + 1, stylesheet.indexOf("}", open));
}

function reducedMotionBlock() {
  const start = stylesheet.indexOf("@media (prefers-reduced-motion: reduce) {");
  expect(start, "prefers-reduced-motion block").toBeGreaterThan(-1);
  // Walk braces so nested rules are included rather than cut at the first }.
  let depth = 0;
  for (let i = stylesheet.indexOf("{", start); i < stylesheet.length; i += 1) {
    if (stylesheet[i] === "{") depth += 1;
    if (stylesheet[i] === "}") {
      depth -= 1;
      if (depth === 0) return stylesheet.slice(start, i + 1);
    }
  }
  throw new Error("unterminated prefers-reduced-motion block");
}

describe("reduced motion", () => {
  const block = reducedMotionBlock();

  it("neutralises animation and transition for every element", () => {
    expect(block).toMatch(/\*\s*,/);
    expect(block).toMatch(/animation-duration:\s*0\.01ms\s*!important/);
    expect(block).toMatch(/transition-duration:\s*0\.01ms\s*!important/);
    expect(block).toMatch(/animation-iteration-count:\s*1\s*!important/);
  });

  it("removes the hover lift rather than merely shortening it", () => {
    // Collapsing duration still leaves a transform applied on hover, so the
    // movement itself has to go.
    expect(block).toMatch(/button:hover\s*\{\s*transform:\s*none/);
  });

  it("covers every animation the stylesheet actually declares", () => {
    // A named keyframe with no reduced-motion answer would still play.
    const declared = [...stylesheet.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]);
    expect(declared.length).toBeGreaterThan(0);
    // The blanket * rule covers them all; assert it is a wildcard and not an
    // enumeration that could drift as keyframes are added.
    expect(block).toContain("*,");
  });
});

describe("focus visibility", () => {
  it("declares one global focus ring driven by tokens", () => {
    const body = ruleBody(":focus-visible {");
    expect(body).toMatch(/outline:\s*var\(--focus-ring-width\)\s+solid\s+var\(--focus-ring-color\)/);
    expect(body).toMatch(/outline-offset:\s*var\(--focus-ring-offset\)/);
  });

  it("never removes focus outlines without providing another", () => {
    // outline:none is only acceptable where a :focus-visible rule restores a
    // ring for keyboard users.
    const suppressors = [...stylesheet.matchAll(/([^{}]+)\{[^}]*outline:\s*none[^}]*\}/g)].map(
      (m) => m[1].trim().split("\n").pop().trim()
    );
    for (const selector of suppressors) {
      const base = selector.replace(/:focus(-within|-visible)?(:not\(:focus-visible\))?$/, "");
      // Either the suppression is itself scoped away from keyboard focus, or
      // the selector provides its own :focus-visible ring.
      expect(
        selector.includes(":not(:focus-visible)") ||
          stylesheet.includes(`${base}:focus-visible`) ||
          stylesheet.includes(`${base} :focus-visible`),
        `${selector} suppresses the outline with no :focus-visible replacement`
      ).toBe(true);
    }
  });
});

describe("skip link", () => {
  const body = ruleBody(".skip-link {");

  it("stays reachable by keyboard while hidden", () => {
    // display:none or visibility:hidden would drop it from the tab order, which
    // defeats the only thing a skip link does.
    expect(body).not.toMatch(/display:\s*none/);
    expect(body).not.toMatch(/visibility:\s*hidden/);
    expect(body).toMatch(/transform:\s*translateY\(-200%\)/);
  });

  it("becomes visible once focused", () => {
    const focused = ruleBody(".skip-link:focus-visible,");
    expect(focused).toMatch(/transform:\s*translateY\(0\)/);
  });

  it("sits above other layers and uses an AA-safe fill", () => {
    expect(body).toMatch(/z-index:\s*var\(--z-toast\)/);
    expect(body).toMatch(/background:\s*var\(--accent-strong\)/);
    expect(body).toMatch(/color:\s*var\(--accent-on-strong\)/);
  });
});
