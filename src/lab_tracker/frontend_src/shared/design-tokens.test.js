import { readFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

import { describe, expect, it } from "vitest";

const stylesheet = readFileSync(
  resolvePath(process.cwd(), "src/lab_tracker/frontend/styles.css"),
  "utf8"
);

/** Custom properties declared in the base :root block (not theme overrides). */
function rootTokens() {
  const start = stylesheet.indexOf(":root {");
  expect(start, "base :root block").toBeGreaterThan(-1);
  const end = stylesheet.indexOf("}", start);
  const block = stylesheet.slice(start, end);
  const tokens = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens[match[1]] = match[2].trim();
  }
  return tokens;
}

/** Substitute var() references recursively, the way the cascade would. */
function resolveToken(value, tokens, depth = 0) {
  if (depth > 20) {
    throw new Error(`var() chain too deep or cyclic: ${value}`);
  }
  return value.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)/g, (_match, name, fallback) => {
    const next = tokens[name] ?? fallback;
    if (next === undefined) {
      throw new Error(`unresolved token ${name}`);
    }
    return resolveToken(String(next).trim(), tokens, depth + 1);
  });
}

const TOKENS = rootTokens();

function resolved(name) {
  const raw = TOKENS[name];
  expect(raw, `token ${name} is declared`).toBeDefined();
  return resolveToken(raw, TOKENS);
}

describe("design tokens", () => {
  // The whole point of the tier split: it must be a rename, not a redesign.
  // These are the literal values the stylesheet shipped before the primitive
  // and register tiers existed. If one of them moves, something rendered
  // differently, and that is a separate decision from introducing tokens.
  const LEGACY_VALUES = {
    "--accent": "#0d8b6f",
    "--accent-soft": "#daf4ec",
    "--bg": "#f6f5f3",
    "--card": "#ffffff",
    "--danger": "#a4202f",
    "--danger-soft": "#ffe6e7",
    "--ink": "#1b1d21",
    "--ink-soft": "#50555f",
    "--line": "#d6d2cb",
    "--shadow": "0 14px 30px rgba(21, 23, 30, 0.08)",
  };

  it.each(Object.entries(LEGACY_VALUES))(
    "resolves %s to its pre-token value",
    (name, expected) => {
      expect(resolved(name)).toBe(expected);
    }
  );

  it("declares a 7-step type scale anchored at 16px", () => {
    const steps = ["--text-2xs", "--text-xs", "--text-sm", "--text-md", "--text-base", "--text-lg", "--text-xl"];
    for (const step of steps) {
      expect(TOKENS[step], step).toBeDefined();
    }
    expect(steps).toHaveLength(7);
    // 1rem is the anchor; browsers default 1rem to 16px.
    expect(resolved("--text-base")).toBe("1rem");
  });

  it("declares spacing sp-1 through sp-7 in increasing order", () => {
    const values = [1, 2, 3, 4, 5, 6, 7].map((step) =>
      Number.parseFloat(resolved(`--sp-${step}`))
    );
    expect(values).toHaveLength(7);
    for (let index = 1; index < values.length; index += 1) {
      expect(values[index], `--sp-${index + 1} exceeds --sp-${index}`).toBeGreaterThan(
        values[index - 1]
      );
    }
  });

  it("declares the radius scale from xs to pill", () => {
    for (const name of [
      "--radius-xs",
      "--radius-sm",
      "--radius-md",
      "--radius-lg",
      "--radius-xl",
      "--radius-2xl",
      "--radius-pill",
      "--radius-circle",
    ]) {
      expect(TOKENS[name], name).toBeDefined();
    }
    expect(resolved("--radius-pill")).toBe("999px");
  });

  it("declares an elevation ramp sharing one shadow channel", () => {
    expect(resolved("--shadow-0")).toBe("none");
    for (const name of ["--shadow-1", "--shadow-2", "--shadow-3"]) {
      // Substituting a bare channel list into rgba() is only correct if the
      // primitive really does expand to three comma-separated numbers.
      expect(resolved(name), name).toMatch(/^0 \d+px \d+px rgba\(21, 23, 30, 0?\.\d+\)$/);
    }
  });

  it("declares motion durations and easings", () => {
    for (const name of ["--duration-fast", "--duration-base", "--duration-slow"]) {
      expect(resolved(name), name).toMatch(/^\d+ms$/);
    }
    for (const name of ["--ease-standard", "--ease-out", "--ease-emphasized"]) {
      expect(TOKENS[name], name).toBeDefined();
    }
  });

  it("declares a five-layer z-index scale in stacking order", () => {
    const layers = ["--z-base", "--z-sticky", "--z-overlay", "--z-sheet", "--z-toast"].map(
      (name) => Number.parseInt(resolved(name), 10)
    );
    for (let index = 1; index < layers.length; index += 1) {
      expect(layers[index]).toBeGreaterThan(layers[index - 1]);
    }
    // The sticky hero already uses 10; keeping it lets that rule migrate as a
    // rename rather than a behaviour change.
    expect(layers[1]).toBe(10);
  });

  it("declares register tokens defaulting to Work values", () => {
    for (const name of [
      "--font-body",
      "--leading-body",
      "--measure",
      "--pad",
      "--gap",
      "--radius-surface",
      "--shadow-surface",
    ]) {
      expect(TOKENS[name], name).toBeDefined();
    }
    // Work defaults, grounded in the values these surfaces already use.
    expect(resolved("--pad")).toBe("1.2rem");
    expect(resolved("--gap")).toBe("0.6rem");
    expect(resolved("--radius-surface")).toBe("18px");
    expect(resolved("--shadow-surface")).toBe("0 14px 30px rgba(21, 23, 30, 0.08)");
  });

  it("keeps the dark theme override pointing at the semantic tier", () => {
    // 83m2.7 rebuilds dark mode properly; until then the override must keep
    // working, which means it overrides semantic names, not primitives.
    const darkStart = stylesheet.indexOf(':root[data-theme="dark"] {');
    expect(darkStart).toBeGreaterThan(-1);
    const darkBlock = stylesheet.slice(darkStart, stylesheet.indexOf("}", darkStart));
    for (const name of ["--bg", "--ink", "--accent", "--shadow"]) {
      expect(darkBlock, `dark override for ${name}`).toContain(`${name}:`);
    }
  });
});
