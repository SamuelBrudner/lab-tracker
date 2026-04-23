import * as React from "react";

import { apiRequest, buildApiPath, fetchAllPages } from "../shared/api.js";

const { useCallback, useEffect, useRef, useState } = React;

function useAnalysisWorkflow({ token, canWrite, selectedProjectId, setBusy, setFlash, enabled = true }) {
  const [analyses, setAnalyses] = useState([]);
  const [visualizations, setVisualizations] = useState([]);

  const [analysisDatasetIds, setAnalysisDatasetIds] = useState([]);
  const [analysisCodeVersion, setAnalysisCodeVersion] = useState("");
  const [analysisMethodHash, setAnalysisMethodHash] = useState("");
  const [analysisEnvironmentHash, setAnalysisEnvironmentHash] = useState("");
  const analysisRequestRef = useRef(0);

  const refreshAnalysisData = useCallback(
    async (projectId) => {
      if (!projectId || !token) {
        setAnalyses([]);
        setVisualizations([]);
        return;
      }

      const requestId = analysisRequestRef.current + 1;
      analysisRequestRef.current = requestId;
      const [nextAnalyses, nextVisualizations] = await Promise.all([
        fetchAllPages(buildApiPath("/analyses", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/visualizations", { project_id: projectId }), { token }),
      ]);
      if (analysisRequestRef.current !== requestId) {
        return;
      }
      setAnalyses(nextAnalyses);
      setVisualizations(nextVisualizations);
    },
    [token]
  );

  useEffect(() => {
    setAnalysisDatasetIds([]);
    setAnalysisCodeVersion("");
    setAnalysisMethodHash("");
    setAnalysisEnvironmentHash("");
  }, [selectedProjectId, token]);

  useEffect(() => {
    if (!enabled || !token || !selectedProjectId) {
      analysisRequestRef.current += 1;
      setAnalyses([]);
      setVisualizations([]);
      return;
    }

    let canceled = false;
    setBusy(true);
    refreshAnalysisData(selectedProjectId)
      .catch((err) => {
        if (!canceled) {
          setFlash("", err.message || "Unable to load analysis data.");
        }
      })
      .finally(() => {
        if (!canceled) {
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [enabled, refreshAnalysisData, selectedProjectId, setBusy, setFlash, token]);

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
    analyses,
    analysisCodeVersion,
    analysisDatasetIds,
    analysisEnvironmentHash,
    analysisMethodHash,
    handleArchiveAnalysis,
    handleCommitAnalysis,
    handleCreateAnalysis,
    refreshAnalysisData,
    setAnalysisCodeVersion,
    setAnalysisDatasetIds,
    setAnalysisEnvironmentHash,
    setAnalysisMethodHash,
    visualizations,
  };
}

export { useAnalysisWorkflow };
