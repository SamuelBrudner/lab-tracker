import * as React from "react";

import { sourceRefText, sourceRegionStyle } from "./format.js";
import {
  artifactDisplayName,
  isFigureArtifact,
  isPointerArtifact,
  isStaleArtifact,
  sanitizeDisplayUri,
  sourceRefsForArtifact,
} from "./source-artifacts.js";

const { useId, useState } = React;

function artifactMetadata(artifact) {
  return artifact?.metadata && typeof artifact.metadata === "object" ? artifact.metadata : {};
}

function ArtifactPreview({ artifact, operation, operationArtifactCount, preview }) {
  const [expanded, setExpanded] = useState(false);
  const previewId = useId();
  const name = artifactDisplayName(artifact);
  const metadata = artifactMetadata(artifact);
  const sourceRefs = operation
    ? sourceRefsForArtifact(operation, artifact, operationArtifactCount)
    : [];
  const regions = sourceRefs
    .map((ref) => ({ ref, style: sourceRegionStyle(ref?.region) }))
    .filter((item) => item.style);
  const codeFile = metadata.run_code_file;
  const codeSymbol = metadata.run_code_symbol;
  const codeLine = metadata.run_code_line;
  const isFigure = isFigureArtifact(artifact) || Boolean(codeFile);
  const contentHash =
    metadata.evidence_content_hash ||
    metadata.figure_content_hash_current ||
    metadata.content_hash_current;
  const sourceUri = sanitizeDisplayUri(
    metadata.evidence_source_uri ||
      metadata.figure_source_uri_current ||
      metadata.source_uri
  );
  const repositoryUri = sanitizeDisplayUri(metadata.run_repo_remote_url);
  const gitCommit = metadata.run_git_commit;
  const hasGitDirty =
    metadata.run_git_dirty !== null && metadata.run_git_dirty !== undefined;
  const gitDirty = metadata.run_git_dirty === true || metadata.run_git_dirty === "true";
  const stale = isStaleArtifact(artifact);
  const previewFreshness = artifact.missing
    ? "Capture metadata unavailable"
    : preview?.status === "error"
      ? "Preview request failed"
      : stale
        ? "Source changed after preview capture"
        : isPointerArtifact(artifact)
          ? "Preview unavailable"
          : "Captured bytes are not marked stale";
  const details = [
    ["Repository", repositoryUri],
    ["Git commit", gitCommit],
    ["Git status", hasGitDirty ? (gitDirty ? "Dirty working tree" : "Clean working tree") : ""],
    ["Code region hash", metadata.run_code_region_hash],
    ["Content hash", contentHash],
    ["Source URI", sourceUri],
    ["Stored checksum", artifact.checksum],
    ["Preview freshness", previewFreshness],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");

  let previewBody;
  if (artifact.missing) {
    previewBody = (
      <p className="source-artifact-state source-artifact-missing" role="status">
        Source capture metadata is unavailable for this reference.
      </p>
    );
  } else if (isPointerArtifact(artifact)) {
    previewBody = (
      <p className="source-artifact-state" role="status">
        Preview unavailable — only a file pointer was captured.
      </p>
    );
  } else if (!String(artifact.content_type || "").startsWith("image/")) {
    previewBody = (
      <p className="source-artifact-state" role="status">
        Inline preview is unavailable for {artifact.content_type || "this source type"}.
      </p>
    );
  } else if (preview?.status === "loading") {
    previewBody = (
      <p className="source-artifact-state" role="status">
        Loading figure preview…
      </p>
    );
  } else if (preview?.status === "error") {
    previewBody = (
      <p className="source-artifact-state source-artifact-fetch-error" role="alert">
        Figure preview could not be loaded. The filename and provenance remain available below.
      </p>
    );
  } else if (preview?.status === "ready" && preview.url) {
    previewBody = (
      <>
        <div
          className={`source-artifact-preview${expanded ? " expanded" : ""}`}
          id={previewId}
        >
          <div className="source-image-frame">
            <img className="source-artifact-image" src={preview.url} alt={`Figure evidence: ${name}`} />
            {regions.map(({ ref, style }, index) => (
              <div
                aria-label={`Source region ${index + 1}: ${ref?.label || name}`}
                className="source-region-box"
                key={`${artifact.note_id || name}-${index}`}
                style={style}
                title={sourceRefText(ref)}
              >
                <span>{index + 1}</span>
              </div>
            ))}
          </div>
        </div>
        <button
          type="button"
          className="btn-link source-artifact-expand"
          aria-controls={previewId}
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? `Collapse ${name}` : `Expand ${name}`}
        </button>
      </>
    );
  } else {
    previewBody = (
      <p className="source-artifact-state" role="status">
        Figure preview is unavailable.
      </p>
    );
  }

  return (
    <article className="review-source-artifact">
      <div className="source-artifact-heading">
        <strong>{name}</strong>
        {artifact.content_type ? <span className="pill">{artifact.content_type}</span> : null}
      </div>
      {stale ? (
        <p className="source-artifact-warning" role="status">
          Preview may be stale — the source file changed after these review bytes were captured.
        </p>
      ) : null}
      {previewBody}
      {codeFile ? (
        <p className="source-artifact-code">
          <span className="subtle">Generated by</span>{" "}
          <span className="mono">{codeFile}</span>
          {codeSymbol ? ` · ${codeSymbol}` : ""}
          {codeLine ? ` · line ${codeLine}` : ""}
        </p>
      ) : sourceUri ? (
        <p className="source-artifact-code">
          <span className="subtle">Source</span>{" "}
          <span className="mono">{sourceUri}</span>
        </p>
      ) : isFigure ? (
        <p className="source-artifact-code subtle">Generating code was not captured for this figure.</p>
      ) : null}
      {details.length > 0 ? (
        <details className="context-details source-artifact-details">
          <summary>Version &amp; file details</summary>
          <dl>
            {details.map(([label, value]) => (
              <React.Fragment key={label}>
                <dt>{label}</dt>
                <dd className="mono">{String(value)}</dd>
              </React.Fragment>
            ))}
          </dl>
        </details>
      ) : null}
    </article>
  );
}

function SourceArtifactEvidence({
  artifacts,
  previews,
  operation = null,
  shared = false,
  sharedMessage = "",
}) {
  if (!artifacts?.length) {
    return null;
  }
  return (
    <section className={shared ? "review-shared-evidence" : "review-source-evidence"}>
      <div className="subtle">{shared ? "Shared source evidence" : "Figure evidence"}</div>
      {shared ? (
        <p className="source-artifact-shared-note">
          {sharedMessage ||
            "This older review did not identify which source belongs to each proposal, so the evidence is shown once for the review."}
        </p>
      ) : null}
      <div className="review-source-artifact-list">
        {artifacts.map((artifact, index) => (
          <ArtifactPreview
            artifact={artifact}
            operation={operation}
            operationArtifactCount={artifacts.length}
            preview={previews?.[artifact.note_id]}
            key={artifact.note_id || artifact.artifact_id || `${artifact.filename}-${index}`}
          />
        ))}
      </div>
    </section>
  );
}

export { SourceArtifactEvidence };
