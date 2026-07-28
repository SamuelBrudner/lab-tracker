import * as React from "react";

import { apiListRequest, buildApiPath } from "../../shared/api.js";
import { formatBytes, formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";
import { DatasetCollectionsSection } from "../collections/index.js";
import { ExperimentChips } from "../experiments/index.js";

const { useEffect, useMemo, useState } = React;
const FILE_PAGE_SIZE = 100;

function DatasetDetailCard({ token, datasetId, projects, navigate, onSetActiveProject }) {
  const [datasetState, setDatasetState] = useState({
    data: null,
    error: "",
    loading: false,
  });
  const dataset = datasetState.data;
  const { error, loading } = datasetState;
  const [filesExpanded, setFilesExpanded] = useState(false);
  const [fileState, setFileState] = useState({
    loading: false,
    loaded: false,
    error: "",
    items: [],
    meta: { limit: FILE_PAGE_SIZE, offset: 0, total: 0 },
  });

  const project = useMemo(() => {
    if (!dataset) {
      return null;
    }
    return projects.find((item) => item.project_id === dataset.project_id) || null;
  }, [projects, dataset]);

  useEffect(() => {
    let canceled = false;
    if (!datasetId) {
      setDatasetState({ data: null, error: "", loading: false });
      return () => {
        canceled = true;
      };
    }
    setDatasetState({ data: null, error: "", loading: true });
    apiListRequest(
      buildApiPath("/datasets/summaries", {
        dataset_id: datasetId,
        limit: 1,
        offset: 0,
      }),
      { token }
    )
      .then(({ data }) => {
        if (canceled) {
          return;
        }
        if (data.length === 0) {
          setDatasetState({
            data: null,
            error: "Dataset not found.",
            loading: false,
          });
          return;
        }
        setDatasetState({ data: data[0], error: "", loading: false });
      })
      .catch((err) => {
        if (!canceled) {
          setDatasetState({
            data: null,
            error: err.message || "Failed to load dataset summary.",
            loading: false,
          });
        }
      });
    return () => {
      canceled = true;
    };
  }, [datasetId, token]);

  useEffect(() => {
    setFilesExpanded(false);
    setFileState({
      loading: false,
      loaded: false,
      error: "",
      items: [],
      meta: { limit: FILE_PAGE_SIZE, offset: 0, total: 0 },
    });
  }, [datasetId, token]);

  async function loadFiles(offset = 0) {
    if (!datasetId) {
      return;
    }
    setFileState((current) => ({ ...current, loading: true, error: "" }));
    try {
      const page = await apiListRequest(
        buildApiPath(`/datasets/${datasetId}/manifest-files`, {
          limit: FILE_PAGE_SIZE,
          offset,
        }),
        { token }
      );
      setFileState({
        loading: false,
        loaded: true,
        error: "",
        items: page.data,
        meta: page.meta,
      });
    } catch (err) {
      setFileState({
        loading: false,
        loaded: true,
        error: err.message || "Failed to load dataset files.",
        items: [],
        meta: { limit: FILE_PAGE_SIZE, offset: 0, total: 0 },
      });
    }
  }

  async function toggleFiles() {
    const nextExpanded = !filesExpanded;
    setFilesExpanded(nextExpanded);
    if (nextExpanded && !fileState.loaded && !fileState.loading) {
      await loadFiles(0);
    }
  }

  const collectionSnapshots = dataset?.collection_snapshots || [];
  const fileOffset = Number(fileState.meta?.offset || 0);
  const fileTotal = Number(fileState.meta?.total ?? fileState.items.length);

  return (
    <article className="card span-8">
      <div className="item-head">
        <h2>Dataset Detail</h2>
        {loading ? <span className="pill">Loading...</span> : null}
      </div>
      {error ? <p className="flash error">{error}</p> : null}

      {dataset ? (
        <div className="stack">
          <div className="inline">
            <span className="pill">{dataset.status}</span>
            {project ? <span className="pill">{project.name}</span> : null}
          </div>

          <div className="stack">
            <div className="subtle">Dataset ID</div>
            <div className="mono">{dataset.dataset_id}</div>
            <div className="subtle">Project ID</div>
            <div className="mono">{dataset.project_id}</div>
            <div className="subtle">Commit hash</div>
            <div className="mono">{dataset.commit_hash}</div>
            <div className="subtle">Created</div>
            <div className="mono">{formatDate(dataset.created_at)}</div>
            <div className="subtle">Updated</div>
            <div className="mono">{formatDate(dataset.updated_at)}</div>
          </div>

          <div className="stack">
            <div className="subtle">Question links</div>
            {(dataset.question_links || []).length === 0 ? (
              <p className="subtle">(none)</p>
            ) : (
              <div className="stack">
                {(dataset.question_links || []).map((link) => (
                  <div className="item" key={`${link.role}:${link.question_id}`}>
                    <div className="item-head">
                      <AppLink
                        to={`/app/questions/${link.question_id}`}
                        navigate={navigate}
                        className="link"
                      >
                        <strong>{link.role}</strong>
                      </AppLink>
                      <span className="pill">{link.outcome_status}</span>
                    </div>
                    <p className="mono">{link.question_id}</p>
                  </div>
                ))}
              </div>
            )}
          </div>

          <ExperimentChips
            token={token}
            entityType="dataset"
            entityId={dataset.dataset_id}
            navigate={navigate}
          />

          <DatasetCollectionsSection
            token={token}
            collectionSnapshots={collectionSnapshots}
          />

          <div className="stack">
            <div className="item-head">
              <h3>Legacy Files</h3>
              <span className="pill">{fileState.loaded ? fileTotal : "not loaded"}</span>
            </div>
            <p className="subtle">
              Load individual legacy files only when needed. Collection members are browsed
              separately in bounded pages above.
            </p>
            <button
              type="button"
              className="btn-secondary"
              aria-expanded={filesExpanded}
              disabled={fileState.loading}
              onClick={toggleFiles}
            >
              {filesExpanded ? "Hide legacy files" : "Show legacy files"}
            </button>
            {filesExpanded ? (
              <div className="stack">
                {fileState.loading ? (
                  <p className="subtle">Loading up to 100 files...</p>
                ) : null}
                {fileState.error ? <p className="flash error">{fileState.error}</p> : null}
                {fileState.loaded &&
                !fileState.loading &&
                !fileState.error &&
                fileState.items.length === 0 ? (
                  <p className="subtle">(no legacy files)</p>
                ) : null}
                {fileState.items.map((file) => (
                  <div className="item" key={file.file_id || file.path}>
                    <div className="item-head">
                      <span className="mono">{file.path}</span>
                      <span className="subtle">{formatBytes(file.size_bytes)}</span>
                    </div>
                    <p className="mono">sha256: {file.checksum}</p>
                  </div>
                ))}
                {fileTotal > FILE_PAGE_SIZE ? (
                  <div className="inline">
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={fileState.loading || fileOffset <= 0}
                      onClick={() => loadFiles(Math.max(0, fileOffset - FILE_PAGE_SIZE))}
                    >
                      Previous files
                    </button>
                    <span className="subtle">
                      {fileOffset + 1}-
                      {Math.min(fileOffset + fileState.items.length, fileTotal)} of {fileTotal}
                    </span>
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={
                        fileState.loading || fileOffset + fileState.items.length >= fileTotal
                      }
                      onClick={() => loadFiles(fileOffset + FILE_PAGE_SIZE)}
                    >
                      Next files
                    </button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="inline detail-actions">
        <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
          Back
        </button>
        {dataset ? (
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              onSetActiveProject(dataset.project_id);
              navigate("/app");
            }}
          >
            Set active project
          </button>
        ) : null}
      </div>
    </article>
  );
}

export { DatasetDetailCard };
