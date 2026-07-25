async function sha256Hex(value) {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error(
      "This browser cannot create a private retry key for the supplied context."
    );
  }
  const digest = await subtle.digest(
    "SHA-256",
    new TextEncoder().encode(String(value))
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}

async function starterContextKeys({ projectId, userId, context }) {
  const digest = await sha256Hex(
    ["lab-tracker-starter-context-v1", projectId, userId, context].join("\0")
  );
  return {
    clientCaptureId: `onboarding-context-${digest}`,
    idempotencyKey: `starter-questions:${digest}`,
  };
}

export { sha256Hex, starterContextKeys };
