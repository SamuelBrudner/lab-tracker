import { readFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

import { describe, expect, it } from "vitest";

const stylesheet = readFileSync(
  resolvePath(process.cwd(), "src/lab_tracker/frontend/styles.css"),
  "utf8"
);

const AA_NORMAL = 4.5;
const AA_LARGE = 3;
const AAA_NORMAL = 7;

/** Custom properties from a declaration block starting at `marker`. */
function tokensFrom(marker) {
  const start = stylesheet.indexOf(marker);
  expect(start, `block ${marker}`).toBeGreaterThan(-1);
  const block = stylesheet.slice(start, stylesheet.indexOf("}", start));
  const tokens = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens[match[1]] = match[2].trim();
  }
  return tokens;
}

function resolveToken(value, tokens, depth = 0) {
  if (depth > 20) {
    throw new Error(`var() chain too deep or cyclic: ${value}`);
  }
  return value.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)/g, (_m, name, fallback) => {
    const next = tokens[name] ?? fallback;
    if (next === undefined) {
      throw new Error(`unresolved token ${name}`);
    }
    return resolveToken(String(next).trim(), tokens, depth + 1);
  });
}

function channel(value) {
  const c = value / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** WCAG 2.x relative luminance. */
function luminance(hex) {
  const clean = hex.trim().replace("#", "");
  expect(clean, `hex colour ${hex}`).toMatch(/^[0-9a-fA-F]{6}$/);
  const [r, g, b] = [0, 2, 4].map((i) => Number.parseInt(clean.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(foreground, background) {
  const a = luminance(foreground);
  const b = luminance(background);
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}

const LIGHT = tokensFrom(":root {");
// Both dark blocks exist: the data-theme one is currently inert, the media query
// ships to anyone whose OS prefers dark. Each must be checked on its own, since
// a fix applied to only one is exactly the kind of miss this test is for.
const DARK_ATTR = tokensFrom(':root[data-theme="dark"] {');
const DARK_MEDIA = (() => {
  const start = stylesheet.indexOf("@media (prefers-color-scheme: dark) {");
  expect(start, "prefers-color-scheme block").toBeGreaterThan(-1);
  const rootStart = stylesheet.indexOf(":root {", start);
  const block = stylesheet.slice(rootStart, stylesheet.indexOf("}", rootStart));
  const tokens = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens[match[1]] = match[2].trim();
  }
  return tokens;
})();

function colour(tokens, name) {
  // A theme block overrides only some tokens; the rest inherit from :root, so
  // fall back the way the cascade does. Resolution still prefers the theme's
  // values, which is what lets a token like --focus-ring-color point at
  // --accent-text once and follow the theme automatically.
  const raw = tokens[name] ?? LIGHT[name];
  expect(raw, `token ${name}`).toBeDefined();
  return resolveToken(raw, { ...LIGHT, ...tokens }, 0);
}

/** Every pairing where one token is rendered against another. */
function pairsFor(tokens) {
  return [
    // Body and card text.
    { fg: "--ink", bg: "--bg", min: AA_NORMAL },
    { fg: "--ink", bg: "--card", min: AA_NORMAL },
    { fg: "--ink-soft", bg: "--bg", min: AA_NORMAL },
    { fg: "--ink-soft", bg: "--card", min: AA_NORMAL },
    // Jade rendered as text — the defect this suite exists for.
    { fg: "--accent-text", bg: "--bg", min: AA_NORMAL },
    { fg: "--accent-text", bg: "--card", min: AA_NORMAL },
    // Text sitting on a jade fill.
    { fg: "--accent-on-strong", bg: "--accent-strong", min: AA_NORMAL },
    // Danger text.
    { fg: "--danger", bg: "--bg", min: AA_NORMAL },
    { fg: "--danger", bg: "--card", min: AA_NORMAL },
    { fg: "--ink", bg: "--danger-soft", min: AA_NORMAL },
    { fg: "--ink", bg: "--accent-soft", min: AA_NORMAL },
    // Non-text UI only needs 3:1.
    { fg: "--accent", bg: "--bg", min: AA_LARGE },
    // A focus indicator must be discernible against whatever it rings. The
    // light jade step is only ~2.3-2.9:1 on dark surfaces, so this pair is what
    // forces the ring colour to follow the theme.
    { fg: "--focus-ring-color", bg: "--bg", min: AA_LARGE },
    { fg: "--focus-ring-color", bg: "--card", min: AA_LARGE },
    // Deliberately NOT asserted here: --line on --card is 1.51:1 in light and
    // 1.31:1 in dark, and --card is only 1.09:1 against --bg, so container
    // edges lean on a very low-contrast border plus a shadow. That is a real
    // question under WCAG 1.4.11, but fixing it means changing the neutral
    // palette and the product's whole visual identity — a separate decision
    // from this jade-text fix. Measured and handed to lab-tracker-83m2.6.
  ].map((pair) => ({
    ...pair,
    ratio: contrast(colour(tokens, pair.fg), colour(tokens, pair.bg)),
  }));
}

describe.each([
  ["light", LIGHT],
  ["dark (data-theme)", DARK_ATTR],
  ["dark (prefers-color-scheme)", DARK_MEDIA],
])("contrast — %s theme", (_label, tokens) => {
  it.each(pairsFor(tokens).map((p) => [p.fg, p.bg, p.min, p.ratio]))(
    "%s on %s meets %s:1",
    (_fg, _bg, min, ratio) => {
      expect(Number(ratio.toFixed(2))).toBeGreaterThanOrEqual(min);
    }
  );
});

describe("jade regression guards", () => {
  it("keeps the decorative jade step out of text roles", () => {
    // --accent (jade 600) is 4.25:1 on white and 3.90:1 on --bg. If a rule ever
    // reintroduces it as a text colour, or as a fill under a foreground, this
    // catches it before the eye does.
    const componentRules = stylesheet.slice(stylesheet.indexOf("* {"));
    expect(componentRules).not.toMatch(/\bcolor:\s*var\(--accent\)\s*;/);
    expect(componentRules).not.toMatch(/\bbackground:\s*var\(--accent\)\s*;/);
  });

  it("pairs every jade fill with its own foreground token", () => {
    const strongUses = stylesheet.match(/background:\s*var\(--accent-strong\)/g) || [];
    const onStrongUses = stylesheet.match(/color:\s*var\(--accent-on-strong\)/g) || [];
    expect(strongUses.length).toBeGreaterThan(0);
    // A fill without its paired foreground would inherit --ink and silently
    // regress, so the two must appear the same number of times.
    expect(onStrongUses.length).toBe(strongUses.length);
  });

  it("documents that the light jade text step is AA but not AAA", () => {
    // 6.21:1 clears AA comfortably and falls short of AAA (7:1). Read-register
    // prose in lab-tracker-83m2.8 needs AAA, so it must not simply reuse this
    // token without re-checking.
    const ratio = contrast(colour(LIGHT, "--accent-text"), colour(LIGHT, "--card"));
    expect(ratio).toBeGreaterThanOrEqual(AA_NORMAL);
    expect(ratio).toBeLessThan(AAA_NORMAL);
  });
});

describe("entity type palette", () => {
  const TYPES = [
    "analysis",
    "claim",
    "dataset",
    "exploration-node",
    "goal",
    "note",
    "question",
    "session",
    "visualization",
  ];

  it.each(TYPES)("%s border clears 3:1 against its own fill and the card", (type) => {
    // A node's edge is the thing separating it from the canvas and from its own
    // fill, so it is non-text UI under WCAG 1.4.11. These sat at 1.98-3.20:1
    // before lab-tracker-83m2.5.
    const border = colour(LIGHT, `--entity-${type}-bd`);
    const fill = colour(LIGHT, `--entity-${type}-bg`);
    expect(Number(contrast(border, fill).toFixed(2))).toBeGreaterThanOrEqual(AA_LARGE);
    expect(Number(contrast(border, colour(LIGHT, "--card")).toFixed(2))).toBeGreaterThanOrEqual(
      AA_LARGE
    );
  });

  it("keeps entity colours out of JavaScript", () => {
    // The palette drifted from the app for as long as it lived in JS literals.
    const source = readFileSync(
      resolvePath(process.cwd(), "src/lab_tracker/frontend_src/features/project-graph.jsx"),
      "utf8"
    );
    expect(source).not.toMatch(/#[0-9a-fA-F]{6}/);
    expect(source).toContain("var(--entity-");
  });

  it("gives every entity type a glyph so type is never hue-alone", () => {
    const source = readFileSync(
      resolvePath(process.cwd(), "src/lab_tracker/frontend_src/features/project-graph.jsx"),
      "utf8"
    );
    const glyphBlock = source.slice(source.indexOf("const TYPE_GLYPHS"));
    for (const type of TYPES) {
      expect(glyphBlock).toContain(`${type.replaceAll("-", "_")}:`);
    }
  });
});
