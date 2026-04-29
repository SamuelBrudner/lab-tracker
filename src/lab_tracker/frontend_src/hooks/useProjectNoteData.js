import * as React from "react";

import { apiListRequest, buildApiPath } from "../shared/api.js";

const { useCallback, useEffect, useRef, useState } = React;

const NOTE_PAGE_SIZE = 5;

function useProjectNoteData({ token, selectedProjectId, setFlash, enabled = true }) {
  const [notes, setNotes] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  const refreshRecentNotes = useCallback(
    async (projectId = selectedProjectId) => {
      if (!projectId) {
        setNotes([]);
        setError("");
        return [];
      }

      const requestId = requestRef.current + 1;
      requestRef.current = requestId;
      setLoading(true);
      setError("");
      try {
        const { data } = await apiListRequest(
          buildApiPath("/notes", {
            limit: NOTE_PAGE_SIZE,
            offset: 0,
            project_id: projectId,
          }),
          { token }
        );
        if (requestRef.current !== requestId) {
          return data;
        }
        const items = Array.isArray(data) ? data : [];
        setNotes(items);
        return items;
      } catch (err) {
        if (requestRef.current === requestId) {
          const nextError = err.message || "Unable to load recent notes.";
          setNotes([]);
          setError(nextError);
          setFlash("", nextError);
        }
        return [];
      } finally {
        if (requestRef.current === requestId) {
          setLoading(false);
        }
      }
    },
    [selectedProjectId, setFlash, token]
  );

  useEffect(() => {
    if (!enabled || !selectedProjectId) {
      requestRef.current += 1;
      setLoading(false);
      setError("");
      setNotes([]);
      return;
    }

    refreshRecentNotes(selectedProjectId);
  }, [enabled, refreshRecentNotes, selectedProjectId]);

  return {
    error,
    loading,
    notes,
    refreshRecentNotes,
  };
}

export { useProjectNoteData };
