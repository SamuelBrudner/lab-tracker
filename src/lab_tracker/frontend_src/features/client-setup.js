const SOURCE_REPOSITORY =
  "https://github.com/SamuelBrudner/lab-tracker.git";
const FULL_GIT_REVISION = /^[0-9a-f]{40}$/i;

function normalizeSourceRevision(sourceRevision) {
  const revision = String(sourceRevision || "").trim().toLowerCase();
  return FULL_GIT_REVISION.test(revision) ? revision : "";
}

function matchingClientSetup(sourceRevision) {
  const revision = normalizeSourceRevision(sourceRevision);
  if (!revision) {
    return null;
  }

  const installRequirement =
    `lab-tracker @ git+${SOURCE_REPOSITORY}@${revision}`;
  return {
    installRequirement,
    projectImportCommand:
      'uv run python -c "import lab_tracker_client; print(\'lab_tracker_client import OK\')"',
    projectInstallCommand: `uv add "${installRequirement}"`,
    revision,
    toolInstallCommand: `uv tool install --force "${installRequirement}"`,
    verifyClientCommand:
      `uv run lt setup verify-client --expected-revision ${revision}`,
    verifyMcpCommand:
      `lt setup verify-mcp --expected-revision ${revision}`,
  };
}

export { matchingClientSetup, normalizeSourceRevision };
