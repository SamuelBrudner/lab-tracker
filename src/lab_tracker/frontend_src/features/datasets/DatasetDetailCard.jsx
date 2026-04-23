import * as React from "react";

import { fetchAllPages } from "../../shared/api.js";
import { formatBytes, formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";
import { useApiResource } from "../../hooks/useApiResource.js";

const { useEffect, useMemo, useState } = React;

function DatasetDetailCard({ token, datasetId, projects, navigate, onSetActiveProject }) {
  const { data: dataset, error, loading } = useApiResource(
    token && datasetId ? `/datasets/${datasetId}` : "",
    token,
    "Failed to load dataset."
  );
  const [fileState, setFileState] = useState({ loading: false, error: "", items: [] });

  const project = useMemo(() => {
    if (!dataset) {
      return null;
    }
    return projects.find((item) => item.project_id === dataset.project_id) || null;
  }, [projects, dataset]);

  useEffect(() => {
    let canceled = false;
    if (!token || !dataset || dataset.status === "committed") {
      setFileState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setFileState((current) => ({ ...current, loading: true, error: "" }));
    fetchAllPages(`/datasets/${dataset.dataset_id}/files`, { token })
      .then((items) => {
        if (!canceled) {
          setFileState({ loading: false, error: "", items: Array.isArray(items) ? items : [] });
        }
      })
      .catch((err) => {
        if (!canceled) {
          setFileState({
            loading: false,
            error: err.message || "Failed to load dataset files.",
            items: [],
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [dataset, token]);

  const committedFiles = dataset?.commit_manifest?.files || [];
  const stagedFiles = fileState.items || [];
  const fileItems = dataset?.status === "committed" ? committedFiles : stagedFiles;

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

          <div className="stack">
            <div className="item-head">
              <h3>Files</h3>
              <span className="pill">
                {dataset.status === "committed" ? "committed" : "staged"} {fileItems.length}
              </span>
            </div>
            {dataset.status !== "committed" && fileState.loading ? (
              <p className="subtle">Loading attached files...</p>
            ) : null}
            {dataset.status !== "committed" && fileState.error ? (
              <p className="flash error">{fileState.error}</p>
            ) : null}
            {fileItems.length === 0 ? (
              <p className="subtle">(no files)</p>
            ) : (
              <div className="stack">
                {fileItems.map((file) => (
                  <div className="item" key={file.file_id || file.path}>
                    <div className="item-head">
                      <span className="mono">{file.path}</span>
                      <span className="subtle">{formatBytes(file.size_bytes)}</span>
                    </div>
                    <p className="mono">sha256: {file.checksum}</p>
                  </div>
                ))}
              </div>
            )}
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
