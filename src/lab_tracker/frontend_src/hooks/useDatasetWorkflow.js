import * as React from "react";

import { apiRequest, fetchAllPages } from "../shared/api.js";

const { useCallback, useEffect, useState } = React;

function useDatasetWorkflow({
  token,
  canWrite,
  selectedProjectId,
  selectedProject,
  questions,
  datasets,
  refreshProjectData,
  setBusy,
  setFlash,
}) {
  const [datasetPrimaryQuestionId, setDatasetPrimaryQuestionId] = useState("");
  const [datasetSecondaryRaw, setDatasetSecondaryRaw] = useState("");
  const [datasetFilesById, setDatasetFilesById] = useState({});
  const [datasetReviewsById, setDatasetReviewsById] = useState({});

  const loadDatasetFiles = useCallback(
    async (datasetId) => {
      if (!token) {
        return null;
      }

      setDatasetFilesById((current) => ({
        ...current,
        [datasetId]: {
          error: "",
          items: current[datasetId]?.items || [],
          loaded: current[datasetId]?.loaded || false,
          loading: true,
        },
      }));

      try {
        const items = await fetchAllPages(`/datasets/${datasetId}/files`, { token });
        const normalized = Array.isArray(items) ? items : [];
        setDatasetFilesById((current) => ({
          ...current,
          [datasetId]: {
            error: "",
            items: normalized,
            loaded: true,
            loading: false,
          },
        }));
        return normalized;
      } catch (err) {
        setDatasetFilesById((current) => ({
          ...current,
          [datasetId]: {
            error: err.message || "Failed to load dataset files.",
            items: current[datasetId]?.items || [],
            loaded: true,
            loading: false,
          },
        }));
        return null;
      }
    },
    [token]
  );

  useEffect(() => {
    setDatasetFilesById({});
    setDatasetReviewsById({});
  }, [selectedProjectId, token]);

  useEffect(() => {
    if (questions.length === 0) {
      setDatasetPrimaryQuestionId("");
      return;
    }
    const hasCurrent = questions.some((item) => item.question_id === datasetPrimaryQuestionId);
    if (!hasCurrent) {
      setDatasetPrimaryQuestionId(questions[0].question_id);
    }
  }, [datasetPrimaryQuestionId, questions]);

  useEffect(() => {
    let canceled = false;
    const reviewRequired = Boolean(
      selectedProject && selectedProject.review_policy && selectedProject.review_policy !== "none"
    );

    if (!token || !selectedProjectId || !reviewRequired) {
      setDatasetReviewsById({});
      return () => {
        canceled = true;
      };
    }

    const stagedDatasets = (datasets || []).filter((dataset) => dataset.status === "staged");
    const datasetIds = stagedDatasets.map((dataset) => dataset.dataset_id);

    if (datasetIds.length === 0) {
      setDatasetReviewsById({});
      return () => {
        canceled = true;
      };
    }

    setDatasetReviewsById((current) => {
      const next = {};
      datasetIds.forEach((datasetId) => {
        next[datasetId] = {
          error: "",
          loading: true,
          review: current[datasetId]?.review || null,
        };
      });
      return next;
    });

    Promise.all(
      datasetIds.map(async (datasetId) => {
        try {
          const review = await apiRequest(`/datasets/${datasetId}/review`, { token });
          return { datasetId, error: "", review };
        } catch (err) {
          if (err && err.status === 404) {
            return { datasetId, error: "", review: null };
          }
          return {
            datasetId,
            error: err.message || "Failed to load review.",
            review: null,
          };
        }
      })
    ).then((results) => {
      if (canceled) {
        return;
      }
      setDatasetReviewsById(() => {
        const next = {};
        results.forEach(({ datasetId, review, error: reviewError }) => {
          next[datasetId] = { error: reviewError, loading: false, review };
        });
        return next;
      });
    });

    return () => {
      canceled = true;
    };
  }, [datasets, selectedProject, selectedProjectId, token]);

  async function handleUploadDatasetFiles(datasetId, files) {
    if (!canWrite) {
      return;
    }
    const selected = Array.isArray(files) ? files : [];
    if (selected.length === 0) {
      setFlash("", "Select at least one file to attach.");
      return;
    }

    let uploadedCount = 0;
    setBusy(true);
    setFlash("", "");
    try {
      for (const file of selected) {
        const formData = new FormData();
        formData.append("file", file);
        await apiRequest(`/datasets/${datasetId}/files`, {
          body: formData,
          method: "POST",
          token,
        });
        uploadedCount += 1;
      }
      setFlash(uploadedCount === 1 ? "Dataset file attached." : "Dataset files attached.");
    } catch (err) {
      setFlash("", err.message || "Failed to attach dataset file.");
    } finally {
      if (uploadedCount > 0) {
        await loadDatasetFiles(datasetId);
      }
      setBusy(false);
    }
  }

  async function handleDeleteDatasetFile(datasetId, fileId) {
    if (!canWrite) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/datasets/${datasetId}/files/${fileId}`, {
        method: "DELETE",
        token,
      });
      await loadDatasetFiles(datasetId);
      setFlash("Dataset file removed.");
    } catch (err) {
      setFlash("", err.message || "Failed to remove dataset file.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateDataset(event) {
    event.preventDefault();
    if (!selectedProjectId || !canWrite) {
      return;
    }
    if (!datasetPrimaryQuestionId) {
      setFlash("", "Pick a primary question for the dataset.");
      return;
    }

    const secondaryQuestionIds = datasetSecondaryRaw
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item && item !== datasetPrimaryQuestionId);

    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest("/datasets", {
        body: {
          primary_question_id: datasetPrimaryQuestionId,
          project_id: selectedProjectId,
          secondary_question_ids: secondaryQuestionIds,
        },
        method: "POST",
        token,
      });
      setDatasetSecondaryRaw("");
      await refreshProjectData(selectedProjectId);
      setFlash("Dataset staged.");
    } catch (err) {
      setFlash("", err.message || "Failed to create dataset.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCommitDataset(datasetId) {
    if (!canWrite) {
      return;
    }
    const fileState = datasetFilesById[datasetId];
    if (
      fileState &&
      fileState.loaded &&
      !fileState.loading &&
      !fileState.error &&
      fileState.items.length === 0
    ) {
      setFlash("", "Attach at least one file before committing a dataset.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const updated = await apiRequest(`/datasets/${datasetId}`, {
        body: { status: "committed" },
        method: "PATCH",
        token,
      });
      await refreshProjectData(selectedProjectId);
      setFlash(
        updated && updated.status === "committed"
          ? "Dataset committed."
          : "Commit requested. Awaiting PI review."
      );
    } catch (err) {
      setFlash("", err.message || "Failed to commit dataset.");
    } finally {
      setBusy(false);
    }
  }

  return {
    datasetFilesById,
    datasetPrimaryQuestionId,
    datasetReviewsById,
    datasetSecondaryRaw,
    handleCommitDataset,
    handleCreateDataset,
    handleDeleteDatasetFile,
    handleUploadDatasetFiles,
    loadDatasetFiles,
    setDatasetPrimaryQuestionId,
    setDatasetSecondaryRaw,
  };
}

export { useDatasetWorkflow };
