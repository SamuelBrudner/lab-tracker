import * as React from "react";

import { fetchProtectedBlobResource } from "../shared/api.js";
import { isPointerArtifact } from "../features/graph-drafts/source-artifacts.js";

const { useEffect, useState } = React;
const MAX_CONCURRENT_SOURCE_PREVIEWS = 4;

function useSourceArtifactPreviews(artifacts, token) {
  const byNoteId = new Map();
  for (const artifact of artifacts || []) {
    const noteId = artifact?.note_id;
    if (
      typeof noteId !== "string" ||
      !noteId ||
      artifact.missing ||
      isPointerArtifact(artifact) ||
      !String(artifact.content_type || "").startsWith("image/")
    ) {
      continue;
    }
    if (!byNoteId.has(noteId)) {
      byNoteId.set(noteId, artifact);
    }
  }
  const loadSignature = JSON.stringify(
    [...byNoteId.values()]
      .map((artifact) => ({
        checksum: artifact.checksum || "",
        content_type: artifact.content_type,
        note_id: artifact.note_id,
      }))
      .sort((left, right) => left.note_id.localeCompare(right.note_id))
  );
  const [previews, setPreviews] = useState({});

  useEffect(() => {
    const loadableArtifacts = JSON.parse(loadSignature);
    let canceled = false;
    const objectUrls = [];
    const initial = {};
    for (const artifact of loadableArtifacts) {
      initial[artifact.note_id] = { status: "loading", url: "" };
    }
    setPreviews(initial);

    let nextArtifactIndex = 0;

    async function loadNextArtifact() {
      while (!canceled) {
        const artifact = loadableArtifacts[nextArtifactIndex];
        nextArtifactIndex += 1;
        if (!artifact) {
          return;
        }
        try {
          const resource = await fetchProtectedBlobResource({
            path: `/notes/${artifact.note_id}/raw`,
            token,
          });
          if (!String(resource.contentType || "").startsWith("image/")) {
            throw new Error("Source response was not an image.");
          }
          const url = URL.createObjectURL(resource.blob);
          if (canceled) {
            URL.revokeObjectURL(url);
            return;
          }
          objectUrls.push(url);
          setPreviews((current) => ({
            ...current,
            [artifact.note_id]: { status: "ready", url },
          }));
        } catch (error) {
          if (!canceled) {
            setPreviews((current) => ({
              ...current,
              [artifact.note_id]: {
                error: error?.message || "Preview could not be loaded.",
                status: "error",
                url: "",
              },
            }));
          }
        }
      }
    }

    const workerCount = Math.min(
      MAX_CONCURRENT_SOURCE_PREVIEWS,
      loadableArtifacts.length
    );
    for (let workerIndex = 0; workerIndex < workerCount; workerIndex += 1) {
      void loadNextArtifact();
    }

    return () => {
      canceled = true;
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [loadSignature, token]);

  return previews;
}

export { useSourceArtifactPreviews };
