import { readFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

import { describe, expect, it } from "vitest";

const rawStylesheet = readFileSync(
  resolvePath(process.cwd(), "src/lab_tracker/frontend/styles.css"),
  "utf8"
);
// The comments here describe the rules and quote the patterns under test, so
// strip them or a comment can pass or fail an assertion on its own.
const stylesheet = rawStylesheet.replace(/\/\*[\s\S]*?\*\//g, "");

/** The Read register's rules, comments removed. */
function readSection() {
  const start = stylesheet.indexOf('[data-register="read"]');
  const end = stylesheet.indexOf(":focus-visible {");
  expect(start, "read register section").toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  return stylesheet.slice(start, end);
}

const REGISTER_TOKENS = [
  "--font-body",
  "--leading-body",
  "--measure",
  "--pad",
  "--gap",
  "--radius-surface",
  "--shadow-surface",
];

/** Declarations inside the [data-register="read"] token block. */
function readTokens() {
  const start = stylesheet.indexOf('[data-register="read"] {');
  expect(start, "read register token block").toBeGreaterThan(-1);
  const block = stylesheet.slice(start, stylesheet.indexOf("}", start));
  const tokens = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens[match[1]] = match[2].trim();
  }
  return tokens;
}

/** Work values, from :root. */
function workTokens() {
  const start = stylesheet.indexOf(":root {");
  const block = stylesheet.slice(start, stylesheet.indexOf("}", start));
  const tokens = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens[match[1]] = match[2].trim();
  }
  return tokens;
}

describe("read register", () => {
  const read = readTokens();
  const work = workTokens();

  it("overrides all seven register tokens", () => {
    for (const token of REGISTER_TOKENS) {
      expect(read[token], token).toBeDefined();
      expect(read[token], `${token} differs from Work`).not.toBe(work[token]);
    }
  });

  it("uses a system serif with no webfont request", () => {
    // A reading surface should not wait on a network font.
    expect(read["--font-body"]).toMatch(/serif/);
    expect(read["--font-body"]).toMatch(/Charter|Iowan|Palatino|Georgia/);
    expect(readSection()).not.toContain("@import");
  });

  it("is calmer and airier than Work", () => {
    expect(Number.parseFloat(read["--leading-body"])).toBeGreaterThan(1.5);
    expect(Number.parseInt(read["--measure"], 10)).toBeLessThanOrEqual(68);
    // Flat surface: Read drops the Work card shadow.
    expect(read["--shadow-surface"]).toContain("shadow-0");
  });

  it("actually consumes the tokens, rather than declaring them unused", () => {
    // The tokens sat defined-but-unused before this; a register that nothing
    // reads is not a register.
    const scoped = stylesheet.slice(stylesheet.indexOf('[data-register="read"] .card'));
    expect(scoped).toContain("padding: var(--pad)");
    expect(scoped).toContain("border-radius: var(--radius-surface)");
    expect(scoped).toContain("box-shadow: var(--shadow-surface)");
    expect(stylesheet).toMatch(/\[data-register="read"\] \.stack \{\s*gap: var\(--gap\)/);
  });

  it("leaves Work untouched by scoping every rule under the attribute", () => {
    // The guarantee that shipping Read changes nothing for existing surfaces:
    // every rule the register introduces is behind [data-register="read"].
    // Split into rules and check each rule's selector list.
    const selectors = readSection()
      .split("}")
      .map((chunk) => chunk.split("{")[0].trim())
      .filter(Boolean);
    expect(selectors.length).toBeGreaterThan(0);
    for (const selector of selectors) {
      for (const part of selector.split(",")) {
        expect(part.trim(), `unscoped selector: ${part.trim()}`).toMatch(
          /^\[data-register="read"\]/
        );
      }
    }
  });

  it("narrows prose but not controls", () => {
    // A measure on inputs, tables or the grid would break layouts rather than
    // aid reading.
    const measureRule = readSection()
      .split("}")
      .map((chunk) => chunk.trim())
      .find((chunk) => chunk.includes("max-width: var(--measure)"));
    expect(measureRule, "a rule applies the measure").toBeDefined();
    const selector = measureRule.split("{")[0];
    expect(selector).toMatch(/\bp\b/);
    expect(selector).not.toMatch(/input|select|textarea|table|\.grid/);
  });
});

describe("register selection", () => {
  const shell = readFileSync(
    resolvePath(process.cwd(), "src/lab_tracker/frontend_src/app-shell.jsx"),
    "utf8"
  );

  it("declares the register once at the shell", () => {
    expect(shell).toContain("data-register={routeRegister(route.kind)}");
  });

  it("defaults every route to Work", () => {
    // No surface opts into Read yet; udv1.3 (Morning Read) is the first.
    expect(shell).toMatch(/const ROUTE_REGISTERS = \{\s*\}/);
    expect(shell).toMatch(/ROUTE_REGISTERS\[kind\] === "read" \? "read" : "work"/);
  });
});
