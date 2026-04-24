import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath, fetchAllPages } from "../shared/api.js";

const { useCallback, useEffect, useRef, useState } = React;

const RECENT_COMMITTED_LIMIT = 5;

function timestampValue(item) {
  return Date.parse(item?.updated_at || item?.created_at || "") || 0;
}

function sortByRecent(items) {
  const nextItems = Array.isArray(items) ? [...items] : [];
  nextItems.sort((left, right) => timestampValue(right) - timestampValue(left));
  return nextItems;
}

function useAnalysisWorkflow({ token, canWrite, selectedProjectId, setBusy, setFlash, enabled = true }) {
  const [stagedAnalyses, setStagedAnalyses] = useState([]);
  const [recentCommittedAnalyses, setRecentCommittedAnalyses] = useState([]);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisError, setAnalysisError] = useState("");
  const [visualizationStates, setVisualizationStates] = useState({});

  const [analysisDatasetIds, setAnalysisDatasetIds] = useState([]);
  const [analysisCodeVersion, setAnalysisCodeVersion] = useState("");
  const [analysisMethodHash, setAnalysisMethodHash] = useState("");
  const [analysisEnvironmentHash, setAnalysisEnvironmentHash] = useState("");

  const analysisRequestRef = useRef(0);
  const visualizationRequestRef = useRef(0);

  const refreshAnalysisData = useCallback(
    async (projectId = selectedProjectId) => {
      if (!projectId || !token) {
        setAnalysisError("");
        setStagedAnalyses([]);
        setRecentCommittedAnalyses([]);
        return { recentCommitted: [], staged: [] };
      }

      const requestId = analysisRequestRef.current + 1;
      analysisRequestRef.current = requestId;
      setAnalysisLoading(true);
      setAnalysisError("");
      try {
        const [nextStagedAnalyses, committedMetaPage] = await Promise.all([
          fetchAllPages(
            buildApiPath("/analyses", {
              project_id: projectId,
              status: "staged",
            }),
            { token }
          ),
          apiListRequest(
            buildApiPath("/analyses", {
              limit: 1,
              offset: 0,
              project_id: projectId,
              status: "committed",
            }),
            { token }
          ),
        ]);

        let nextCommittedAnalyses = [];
        const committedTotal = committedMetaPage.meta.total || 0;
        if (committedTotal > 0) {
          const recentOffset = Math.max(committedTotal - RECENT_COMMITTED_LIMIT, 0);
          const committedPage =
            committedTotal === 1
              ? committedMetaPage
              : await apiListRequest(
                  buildApiPath("/analyses", {
                    limit: RECENT_COMMITTED_LIMIT,
                    offset: recentOffset,
                    project_id: projectId,
                    status: "committed",
                  }),
                  { token }
                );
          nextCommittedAnalyses = committedPage.data;
        }

        if (analysisRequestRef.current !== requestId) {
          return {
            recentCommitted: nextCommittedAnalyses,
            staged: nextStagedAnalyses,
          };
        }

        setStagedAnalyses(sortByRecent(nextStagedAnalyses));
        setRecentCommittedAnalyses(sortByRecent(nextCommittedAnalyses));
        return {
          recentCommitted: nextCommittedAnalyses,
          staged: nextStagedAnalyses,
        };
      } catch (err) {
        if (analysisRequestRef.current === requestId) {
          const nextError = err.message || "Unable to load analysis work.";
          setAnalysisError(nextError);
          setStagedAnalyses([]);
          setRecentCommittedAnalyses([]);
          setFlash("", nextError);
        }
        return {
          recentCommitted: [],
          staged: [],
        };
      } finally {
        if (analysisRequestRef.current === requestId) {
          setAnalysisLoading(false);
        }
      }
    },
    [selectedProjectId, setFlash, token]
  );

  const loadVisualizations = useCallback(
    async (analysisId, { force = false } = {}) => {
      if (!analysisId || !token) {
        return [];
      }

      const currentState = visualizationStates[analysisId];
      if (
        !force &&
        currentState &&
        currentState.loaded &&
        !currentState.loading &&
        !currentState.error
      ) {
        return currentState.items;
      }

      const requestId = visualizationRequestRef.current + 1;
      visualizationRequestRef.current = requestId;
      setVisualizationStates((current) => ({
        ...current,
        [analysisId]: {
          error: "",
          items: current[analysisId]?.items || [],
          loaded: current[analysisId]?.loaded || false,
          loading: true,
        },
      }));

      try {
        const items = await fetchAllPages(
          buildApiPath("/visualizations", { analysis_id: analysisId }),
          { token }
        );
        if (visualizationRequestRef.current !== requestId) {
          return items;
        }
        const nextItems = Array.isArray(items) ? sortByRecent(items) : [];
        setVisualizationStates((current) => ({
          ...current,
          [analysisId]: {
            error: "",
            items: nextItems,
            loaded: true,
            loading: false,
          },
        }));
        return nextItems;
      } catch (err) {
        if (visualizationRequestRef.current === requestId) {
          setVisualizationStates((current) => ({
            ...current,
            [analysisId]: {
              error: err.message || "Failed to load visualizations.",
              items: current[analysisId]?.items || [],
              loaded: true,
              loading: false,
            },
          }));
        }
        return [];
      }
    },
    [token, visualizationStates]
  );

  useEffect(() => {
    setAnalysisDatasetIds([]);
    setAnalysisCodeVersion("");
    setAnalysisMethodHash("");
    setAnalysisEnvironmentHash("");
    setVisualizationStates({});
  }, [selectedProjectId, token]);

  useEffect(() => {
    if (!enabled || !token || !selectedProjectId) {
      analysisRequestRef.current += 1;
      setAnalysisLoading(false);
      setAnalysisError("");
      setStagedAnalyses([]);
      setRecentCommittedAnalyses([]);
      return;
    }

    refreshAnalysisData(selectedProjectId);
  }, [enabled, refreshAnalysisData, selectedProjectId, token]);

  async function handleCreateAnalysis(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (analysisDatasetIds.length === 0) {
      setFlash("", "Select at least one dataset for the analysis.");
      return;
    }
    if (!analysisCodeVersion.trim()) {
      setFlash("", "code_version is required.");
      return;
    }
    if (!analysisMethodHash.trim()) {
      setFlash("", "method_hash is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest("/analyses", {
        body: {
          code_version: analysisCodeVersion.trim(),
          dataset_ids: analysisDatasetIds,
          environment_hash: analysisEnvironmentHash.trim() || null,
          method_hash: analysisMethodHash.trim(),
          project_id: selectedProjectId,
        },
        method: "POST",
        token,
      });
      setAnalysisDatasetIds([]);
      setAnalysisCodeVersion("");
      setAnalysisMethodHash("");
      setAnalysisEnvironmentHash("");
      await refreshAnalysisData(selectedProjectId);
      setFlash("Analysis staged.");
    } catch (err) {
      setFlash("", err.message || "Failed to create analysis.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCommitAnalysis(analysisId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/analyses/${analysisId}/commit`, {
        body: {},
        method: "POST",
        token,
      });
      await refreshAnalysisData(selectedProjectId);
      setFlash("Analysis committed.");
    } catch (err) {
      setFlash("", err.message || "Failed to commit analysis.");
    } finally {
      setBusy(false);
    }
  }

  async function handleArchiveAnalysis(analysisId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/analyses/${analysisId}`, {
        body: { status: "archived" },
        method: "PATCH",
        token,
      });
      await refreshAnalysisData(selectedProjectId);
      setFlash("Analysis archived.");
    } catch (err) {
      setFlash("", err.message || "Failed to archive analysis.");
    } finally {
      setBusy(false);
    }
  }

  return {
    analysisCodeVersion,
    analysisDatasetIds,
    analysisEnvironmentHash,
    analysisError,
    analysisLoading,
    analysisMethodHash,
    handleArchiveAnalysis,
    handleCommitAnalysis,
    handleCreateAnalysis,
    loadVisualizations,
    recentCommittedAnalyses,
    refreshAnalysisData,
    setAnalysisCodeVersion,
    setAnalysisDatasetIds,
    setAnalysisEnvironmentHash,
    setAnalysisMethodHash,
    stagedAnalyses,
    visualizationStates,
  };
}

export { useAnalysisWorkflow };
