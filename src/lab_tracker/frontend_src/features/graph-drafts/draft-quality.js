// Compose the one advisory sentence the review page shows from the project's
// draft-quality ledger. The ledger is a read of the review record (per
// provider x model x prompt version); the sentence is built here so the
// server keeps serving numbers, not prose.

function matchesDraft(row, changeSet) {
  return (
    row.provider === changeSet?.provider &&
    row.model === changeSet?.model &&
    row.prompt_version === changeSet?.prompt_version
  );
}

function sum(rows, field) {
  return rows.reduce((total, row) => total + Number(row[field] || 0), 0);
}

function pluralize(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

// Returns "" when the ledger has nothing to say about this draft's
// provider/model/prompt (a first draft has no history to lean on).
function draftQualitySummaryLine(ledger, changeSet) {
  const groups = (ledger?.groups || []).filter((group) => matchesDraft(group, changeSet));
  const cells = (ledger?.cells || []).filter((cell) => matchesDraft(cell, changeSet));
  const reviewed = sum(groups, "change_set_count");
  if (reviewed === 0) {
    return "";
  }
  const proposed = sum(cells, "proposed");
  const accepted = sum(cells, "accepted_total");
  const rejected = sum(cells, "rejected");
  const clarifications = sum(groups, "change_sets_with_clarifications");
  const parts = [
    `Across ${pluralize(reviewed, "earlier review")} from ${changeSet.provider}/${changeSet.model}`,
    `(${changeSet.prompt_version}) you kept ${accepted} of ${pluralize(proposed, "proposal")}`,
  ];
  let sentence = `${parts.join(" ")}`;
  if (rejected > 0) {
    sentence += ` and rejected ${rejected}`;
  }
  sentence += ".";
  if (clarifications > 0) {
    sentence += ` ${pluralize(clarifications, "review")} asked you for clarification.`;
  }
  return sentence;
}

export { draftQualitySummaryLine };
