import * as React from "react";

import { apiListRequest, buildApiPath } from "../../shared/api.js";
import { formatBytes, formatDate } from "../../shared/formatters.js";
import { CollectionSnapshotBrowser } from "./CollectionSnapshotBrowser.jsx";

const { useState } = React;

function snapshotIdOf(snapshot) {
  return snapshot?.snapshot_id || snapshot?.collection_snapshot_id || "";
}

function currentSnapshotOf(collection) {
  return collection?.current_snapshot || collection?.current_snapshot_summary || null;
}

function summaryValue(collection, snapshot, name) {
  const flattenedName = `current_${name}`;
  return snapshot?.[name] ?? collection?.[flattenedName] ?? collection?.[name] ?? null;
}

function SnapshotSummary({ snapshot, token, collectionKey }) {
  const snapshotId = snapshotIdOf(snapshot);
  const complete = snapshot?.complete ?? snapshot?.is_complete;
  return (
    <div className="item">
      <div className="item-head">
        <strong>{snapshotId ? `Snapshot ${snapshotId}` : "Current snapshot"}</strong>
        {typeof complete === "boolean" ? (
          <span className="pill">{complete ? "complete" : "incomplete"}</span>
        ) : null}
      </div>
      <div className="inline">
        <span className="pill">{Number(snapshot?.member_count || 0)} members</span>
        <span className="pill">{formatBytes(snapshot?.total_size_bytes)}</span>
        {snapshot?.observed_at ? (
          <span className="subtle">{formatDate(snapshot.observed_at)}</span>
        ) : null}
      </div>
      {snapshot?.manifest_hash ? (
        <p className="mono">manifest: {snapshot.manifest_hash}</p>
      ) : null}
      {snapshot?.source_provider || snapshot?.source_uri ? (
        <p className="mono">
          {[snapshot.source_provider, snapshot.source_uri].filter(Boolean).join(" · ")}
        </p>
      ) : null}
      <CollectionSnapshotBrowser
        token={token}
        snapshotId={snapshotId}
        memberCount={snapshot?.member_count}
        manifestFilename={`${collectionKey || "collection"}-manifest.json`}
      />
    </div>
  );
}

function CollectionCard({ collection, token }) {
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [historyState, setHistoryState] = useState({
    error: "",
    items: [],
    loaded: false,
    loading: false,
    meta: { limit: 20, offset: 0, total: 0 },
  });
  const currentSnapshot = currentSnapshotOf(collection);
  const currentSnapshotId =
    snapshotIdOf(currentSnapshot) || collection?.current_snapshot_id || "";
  const collectionId = collection?.collection_id || collection?.acquisition_collection_id || "";
  const collectionKey = collection?.collection_key || collection?.key || "collection";
  const complete = summaryValue(collection, currentSnapshot, "complete");
  const normalizedCurrentSnapshot = currentSnapshotId
    ? {
        ...currentSnapshot,
        complete,
        manifest_hash: summaryValue(collection, currentSnapshot, "manifest_hash"),
        member_count: summaryValue(collection, currentSnapshot, "member_count"),
        observed_at: summaryValue(collection, currentSnapshot, "observed_at"),
        snapshot_id: currentSnapshotId,
        total_size_bytes: summaryValue(collection, currentSnapshot, "total_size_bytes"),
      }
    : null;

  async function loadHistory(offset = 0) {
    if (!collectionId) {
      return;
    }
    setHistoryState((current) => ({ ...current, error: "", loading: true }));
    try {
      const page = await apiListRequest(
        buildApiPath(`/collections/${collectionId}/snapshots`, {
          limit: 20,
          offset,
        }),
        { token }
      );
      setHistoryState({
        error: "",
        items: page.data,
        loaded: true,
        loading: false,
        meta: page.meta,
      });
    } catch (err) {
      setHistoryState({
        error: err.message || "Failed to load snapshot history.",
        items: [],
        loaded: true,
        loading: false,
        meta: { limit: 20, offset: 0, total: 0 },
      });
    }
  }

  async function toggleHistory() {
    const nextExpanded = !historyExpanded;
    setHistoryExpanded(nextExpanded);
    if (nextExpanded && !historyState.loading && !historyState.loaded) {
      await loadHistory(0);
    }
  }

  const olderSnapshots = historyState.items.filter(
    (snapshot) => snapshotIdOf(snapshot) !== currentSnapshotId
  );
  const historyOffset = Number(historyState.meta?.offset || 0);
  const historyTotal = Number(historyState.meta?.total ?? historyState.items.length);

  return (
    <article className="item">
      <div className="item-head">
        <strong>{collectionKey}</strong>
        {currentSnapshotId ? (
          <span className="pill">{complete ? "complete" : "incomplete"}</span>
        ) : (
          <span className="pill">no snapshot</span>
        )}
      </div>
      <div className="inline">
        <span className="pill">
          {Number(summaryValue(collection, currentSnapshot, "member_count") || 0)} members
        </span>
        <span className="pill">
          {formatBytes(summaryValue(collection, currentSnapshot, "total_size_bytes"))}
        </span>
        {summaryValue(collection, currentSnapshot, "observed_at") ? (
          <span className="subtle">
            {formatDate(summaryValue(collection, currentSnapshot, "observed_at"))}
          </span>
        ) : null}
      </div>
      {summaryValue(collection, currentSnapshot, "source_provider") ||
      summaryValue(collection, currentSnapshot, "source_uri") ? (
        <p className="mono">
          {[
            summaryValue(collection, currentSnapshot, "source_provider"),
            summaryValue(collection, currentSnapshot, "source_uri"),
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      ) : null}
      {summaryValue(collection, currentSnapshot, "manifest_hash") ? (
        <p className="mono">
          manifest: {summaryValue(collection, currentSnapshot, "manifest_hash")}
        </p>
      ) : null}

      {normalizedCurrentSnapshot ? (
        <CollectionSnapshotBrowser
          token={token}
          snapshotId={currentSnapshotId}
          memberCount={normalizedCurrentSnapshot.member_count}
          manifestFilename={`${collectionKey}-manifest.json`}
        />
      ) : null}

      <button
        type="button"
        className="btn-secondary"
        disabled={!collectionId}
        aria-expanded={historyExpanded}
        onClick={toggleHistory}
      >
        {historyExpanded ? "Hide snapshot history" : "Snapshot history"}
      </button>

      {historyExpanded ? (
        <div className="stack">
          {historyState.loading ? <p className="subtle">Loading snapshots...</p> : null}
          {historyState.error ? <p className="flash error">{historyState.error}</p> : null}
          {!historyState.loading &&
          !historyState.error &&
          historyState.items.length === 0 ? (
            <p className="subtle">(no snapshots)</p>
          ) : null}
          {olderSnapshots.map((snapshot) => (
            <SnapshotSummary
              key={snapshotIdOf(snapshot)}
              snapshot={snapshot}
              token={token}
              collectionKey={collectionKey}
            />
          ))}
          {historyState.items.length > 0 && olderSnapshots.length === 0 ? (
            <p className="subtle">No older snapshots.</p>
          ) : null}
          {historyTotal > 20 ? (
            <div className="inline">
              <button
                type="button"
                className="btn-secondary"
                disabled={historyState.loading || historyOffset <= 0}
                onClick={() => loadHistory(Math.max(0, historyOffset - 20))}
              >
                Previous snapshots
              </button>
              <span className="subtle">
                {historyOffset + 1}-
                {Math.min(historyOffset + historyState.items.length, historyTotal)} of{" "}
                {historyTotal}
              </span>
              <button
                type="button"
                className="btn-secondary"
                disabled={
                  historyState.loading ||
                  historyOffset + historyState.items.length >= historyTotal
                }
                onClick={() => loadHistory(historyOffset + 20)}
              >
                Next snapshots
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

export { CollectionCard, SnapshotSummary, snapshotIdOf };
