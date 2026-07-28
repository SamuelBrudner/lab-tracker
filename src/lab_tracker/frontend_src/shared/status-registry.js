/**
 * One place that says what a status means and how it should look.
 *
 * Before this, five separate helpers (roleClass, sessionTypeClass, two local
 * statusClass variants, flagClass) each mapped their own vocabulary onto the
 * same handful of pastel hexes, so the same amber meant "pending" in one
 * surface and "viewer" in another, and adding a status meant editing CSS in a
 * place nobody could find from the call site.
 *
 * Everything now resolves to one of five tones. Colour is never the only
 * carrier: each tone also has a typographic glyph, and every pill gets an
 * aria-label naming its family, so a status survives greyscale, colour
 * blindness, and a screen reader.
 */

/** Feedback tones, plus a neutral for classifications that are not feedback. */
const TONES = ["neutral", "info", "success", "warning", "danger"];

/** Typographic, not an icon set — these render in any font at any size. */
const TONE_GLYPHS = {
  danger: "✗",
  info: "i",
  neutral: "·",
  success: "✓",
  warning: "!",
};

/**
 * family -> value -> { label, tone }.
 *
 * Values are the raw API vocabulary; the label is what a person reads. Where a
 * family has synonyms (accepted/applied/committed all mean "this is in"), they
 * map to the same tone deliberately.
 */
const STATUS_REGISTRY = {
  flag: {
    critical: { label: "Critical", tone: "danger" },
    info: { label: "Info", tone: "info" },
    warning: { label: "Warning", tone: "warning" },
  },
  // Roles are a classification rather than feedback, so only the privileged
  // ones take a tone; a viewer is not a warning, whatever the old amber implied.
  role: {
    admin: { label: "Admin", tone: "info" },
    contributor: { label: "Contributor", tone: "success" },
    editor: { label: "Editor", tone: "success" },
    owner: { label: "Owner", tone: "info" },
    viewer: { label: "Viewer", tone: "neutral" },
  },
  review: {
    accepted: { label: "Accepted", tone: "success" },
    applied: { label: "Applied", tone: "success" },
    changes_requested: { label: "Changes requested", tone: "warning" },
    committed: { label: "Committed", tone: "success" },
    failed: { label: "Failed", tone: "danger" },
    pending: { label: "Pending", tone: "warning" },
    proposed: { label: "Proposed", tone: "warning" },
    ready: { label: "Ready", tone: "warning" },
    rejected: { label: "Rejected", tone: "danger" },
    submitted: { label: "Submitted", tone: "warning" },
  },
  session: {
    operational: { label: "Operational", tone: "info" },
    scientific: { label: "Scientific", tone: "success" },
  },
};

/** Human-readable family names, used in the aria-label. */
const FAMILY_LABELS = {
  flag: "Flag",
  role: "Role",
  review: "Status",
  session: "Session type",
};

function humanize(value) {
  const text = String(value ?? "").replaceAll("_", " ").trim();
  if (!text) {
    return "Unknown";
  }
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Resolve a status to everything a pill needs to render.
 *
 * An unknown value degrades to a neutral pill carrying the raw value rather
 * than throwing or rendering blank: a new API status should look plain, not
 * break the page or silently disappear.
 */
function statusPill(family, value) {
  const entry = STATUS_REGISTRY[family]?.[value];
  const tone = entry?.tone || "neutral";
  const label = entry?.label || humanize(value);
  const familyLabel = FAMILY_LABELS[family] || humanize(family);
  return {
    ariaLabel: `${familyLabel}: ${label}`,
    className: `pill tone-${tone}`,
    glyph: TONE_GLYPHS[tone],
    label,
    tone,
  };
}

export { FAMILY_LABELS, STATUS_REGISTRY, statusPill, TONE_GLYPHS, TONES };
