function formatDate(value) {
  if (!value) {
    return "";
  }
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function formatBytes(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) {
    return String(value);
  }
  if (size < 1024) {
    return `${size} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let next = size;
  let unitIndex = -1;
  while (next >= 1024 && unitIndex < units.length - 1) {
    next /= 1024;
    unitIndex += 1;
  }
  const rounded = next >= 10 ? Math.round(next) : Math.round(next * 10) / 10;
  return `${rounded} ${units[unitIndex]}`;
}

function formatConfidence(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "";
  }
  const clamped = Math.max(0, Math.min(1, value));
  return `${Math.round(clamped * 100)}%`;
}

function buildHighlightedSnippet(noteText, candidateText) {
  const note = String(noteText || "");
  const candidate = String(candidateText || "");
  if (!note || !candidate) {
    return null;
  }

  const lowerNote = note.toLowerCase();
  const lowerCandidate = candidate.toLowerCase();

  let matchIndex = lowerNote.indexOf(lowerCandidate);
  let matchLength = candidate.length;

  if (matchIndex < 0 && candidate.endsWith("?")) {
    const trimmed = candidate.slice(0, -1).trim();
    if (trimmed) {
      const lowerTrimmed = trimmed.toLowerCase();
      const trimmedIndex = lowerNote.indexOf(lowerTrimmed);
      if (trimmedIndex >= 0) {
        matchIndex = trimmedIndex;
        matchLength = trimmed.length;
      }
    }
  }

  if (matchIndex < 0) {
    const tokens = candidate.match(/[a-z0-9]{4,}/gi) || [];
    let bestToken = "";
    let bestIndex = -1;
    for (const token of tokens.slice(0, 8)) {
      const tokenIndex = lowerNote.indexOf(token.toLowerCase());
      if (tokenIndex >= 0 && (bestIndex < 0 || tokenIndex < bestIndex)) {
        bestToken = token;
        bestIndex = tokenIndex;
      }
    }
    if (bestIndex >= 0) {
      matchIndex = bestIndex;
      matchLength = bestToken.length;
    }
  }

  if (matchIndex < 0 || matchLength <= 0) {
    return null;
  }

  const contextChars = 80;
  const start = Math.max(0, matchIndex - contextChars);
  const end = Math.min(note.length, matchIndex + matchLength + contextChars);

  const prefix = note.slice(start, matchIndex);
  const match = note.slice(matchIndex, matchIndex + matchLength);
  const suffix = note.slice(matchIndex + matchLength, end);

  return {
    prefix: start > 0 ? `...${prefix}` : prefix,
    match,
    suffix: end < note.length ? `${suffix}...` : suffix,
  };
}

function roleClass(role) {
  return `pill role-${role || "viewer"}`;
}

function sessionTypeClass(sessionType) {
  return `pill session-type session-${sessionType || "scientific"}`;
}

export {
  buildHighlightedSnippet,
  formatBytes,
  formatConfidence,
  formatDate,
  roleClass,
  sessionTypeClass,
};
