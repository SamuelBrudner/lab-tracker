// The seven structured reasons a reviewer can attach to a rejection. The
// values are the server's GraphOperationRejectReason enum, sent as
// `reject_reason` on the same PATCH that sets `status: "rejected"`; the key
// is the digit the keyboard loop maps to each one.
const REJECT_REASONS = Object.freeze([
  { key: "1", value: "duplicate_of_existing", label: "Duplicate of existing" },
  { key: "2", value: "wrong_target", label: "Wrong target" },
  { key: "3", value: "unsupported_by_source", label: "Unsupported by source" },
  { key: "4", value: "already_captured", label: "Already captured" },
  { key: "5", value: "not_relevant", label: "Not relevant" },
  { key: "6", value: "not_now", label: "Not now" },
  { key: "7", value: "other", label: "Other" },
]);

function reasonForKey(reasons, key) {
  return reasons.find((reason) => reason.key === key) || null;
}

export { REJECT_REASONS, reasonForKey };
