import * as React from "react";

import { buildApiPath, fetchAllPages } from "../shared/api.js";

const { useCallback, useEffect, useRef, useState } = React;

function useProjectSessionData({ token, selectedProjectId, setFlash, enabled = true }) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  const refreshActiveSessions = useCallback(
    async (projectId = selectedProjectId) => {
      if (!projectId) {
        setSessions([]);
        setError("");
        return [];
      }

      const requestId = requestRef.current + 1;
      requestRef.current = requestId;
      setLoading(true);
      setError("");
      try {
        const items = await fetchAllPages(
          buildApiPath("/sessions", {
            project_id: projectId,
            status: "active",
          }),
          { token }
        );
        if (requestRef.current !== requestId) {
          return items;
        }
        const nextItems = Array.isArray(items) ? items : [];
        setSessions(nextItems);
        return nextItems;
      } catch (err) {
        if (requestRef.current === requestId) {
          const nextError = err.message || "Unable to load active sessions.";
          setSessions([]);
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
      setSessions([]);
      return;
    }

    refreshActiveSessions(selectedProjectId);
  }, [enabled, refreshActiveSessions, selectedProjectId]);

  return {
    error,
    loading,
    refreshActiveSessions,
    sessions,
    setSessions,
  };
}

export { useProjectSessionData };
