import * as React from "react";

import { apiListRequest, buildApiPath } from "../../shared/api.js";
import { CollectionCard, SnapshotSummary } from "./CollectionCard.jsx";

const { useEffect, useState } = React;
const COLLECTION_PAGE_SIZE = 20;

function SessionCollectionsSection({ token, sessionId }) {
  const [state, setState] = useState({
    error: "",
    items: [],
    loading: false,
    meta: { limit: COLLECTION_PAGE_SIZE, offset: 0, total: 0 },
  });

  async function loadPage(offset = 0) {
    if (!sessionId) {
      return;
    }
    setState((current) => ({ ...current, error: "", loading: true }));
    try {
      const page = await apiListRequest(
        buildApiPath(`/sessions/${sessionId}/collections`, {
          limit: COLLECTION_PAGE_SIZE,
          offset,
        }),
        { token }
      );
      setState({
        error: "",
        items: page.data,
        loading: false,
        meta: page.meta,
      });
    } catch (err) {
      setState({
        error: err.message || "Failed to load acquisition collections.",
        items: [],
        loading: false,
        meta: { limit: COLLECTION_PAGE_SIZE, offset: 0, total: 0 },
      });
    }
  }

  useEffect(() => {
    let canceled = false;
    if (!sessionId) {
      setState({
        error: "",
        items: [],
        loading: false,
        meta: { limit: COLLECTION_PAGE_SIZE, offset: 0, total: 0 },
      });
      return () => {
        canceled = true;
      };
    }

    setState((current) => ({ ...current, error: "", loading: true }));
    apiListRequest(
      buildApiPath(`/sessions/${sessionId}/collections`, {
        limit: COLLECTION_PAGE_SIZE,
        offset: 0,
      }),
      { token }
    )
      .then((page) => {
        if (!canceled) {
          setState({
            error: "",
            items: page.data,
            loading: false,
            meta: page.meta,
          });
        }
      })
      .catch((err) => {
        if (!canceled) {
          setState({
            error: err.message || "Failed to load acquisition collections.",
            items: [],
            loading: false,
            meta: { limit: COLLECTION_PAGE_SIZE, offset: 0, total: 0 },
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [sessionId, token]);

  const offset = Number(state.meta?.offset || 0);
  const total = Number(state.meta?.total ?? state.items.length);
  return (
    <div className="stack">
      <div className="item-head">
        <h3>Acquisition Collections</h3>
        <span className="pill">{total}</span>
      </div>
      {state.loading ? <p className="subtle">Loading collection summaries...</p> : null}
      {state.error ? <p className="flash error">{state.error}</p> : null}
      {!state.loading && !state.error && state.items.length === 0 ? (
        <p className="subtle">(no collections)</p>
      ) : null}
      <div className="stack">
        {state.items.map((collection) => (
          <CollectionCard
            key={collection.collection_id || collection.acquisition_collection_id}
            collection={collection}
            token={token}
          />
        ))}
      </div>
      {total > COLLECTION_PAGE_SIZE ? (
        <div className="inline">
          <button
            type="button"
            className="btn-secondary"
            disabled={state.loading || offset <= 0}
            onClick={() => loadPage(Math.max(0, offset - COLLECTION_PAGE_SIZE))}
          >
            Previous collections
          </button>
          <span className="subtle">
            {offset + 1}-{Math.min(offset + state.items.length, total)} of {total}
          </span>
          <button
            type="button"
            className="btn-secondary"
            disabled={state.loading || offset + state.items.length >= total}
            onClick={() => loadPage(offset + COLLECTION_PAGE_SIZE)}
          >
            Next collections
          </button>
        </div>
      ) : null}
    </div>
  );
}

function DatasetCollectionsSection({ token, collectionSnapshots = [] }) {
  return (
    <div className="stack">
      <div className="item-head">
        <h3>Acquisition Collections</h3>
        <span className="pill">{collectionSnapshots.length}</span>
      </div>
      {collectionSnapshots.length === 0 ? (
        <p className="subtle">(no collection snapshots)</p>
      ) : (
        <div className="stack">
          {collectionSnapshots.map((snapshot) => (
            <SnapshotSummary
              key={snapshot.snapshot_id || snapshot.collection_snapshot_id}
              snapshot={snapshot}
              token={token}
              collectionKey={snapshot.collection_key}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export { DatasetCollectionsSection, SessionCollectionsSection };
