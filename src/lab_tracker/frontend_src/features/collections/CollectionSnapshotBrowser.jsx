import * as React from "react";

import {
  apiListRequest,
  buildApiPath,
  downloadProtectedResource,
} from "../../shared/api.js";
import { formatBytes } from "../../shared/formatters.js";

const { useEffect, useState } = React;
const MEMBER_PAGE_SIZE = 100;

function CollectionSnapshotBrowser({
  token,
  snapshotId,
  memberCount = 0,
  manifestFilename = "",
}) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [error, setError] = useState("");
  const [items, setItems] = useState([]);
  const [meta, setMeta] = useState({
    limit: MEMBER_PAGE_SIZE,
    offset: 0,
    total: Number(memberCount || 0),
  });
  const [searchInput, setSearchInput] = useState("");
  const [activeSearch, setActiveSearch] = useState("");

  useEffect(() => {
    setExpanded(false);
    setLoading(false);
    setError("");
    setItems([]);
    setMeta({
      limit: MEMBER_PAGE_SIZE,
      offset: 0,
      total: Number(memberCount || 0),
    });
    setSearchInput("");
    setActiveSearch("");
  }, [memberCount, snapshotId]);

  async function loadMembers({ offset = 0, q = activeSearch } = {}) {
    if (!snapshotId) {
      return;
    }
    setLoading(true);
    setError("");
    try {
      const page = await apiListRequest(
        buildApiPath(`/collection-snapshots/${snapshotId}/members`, {
          limit: MEMBER_PAGE_SIZE,
          offset,
          q,
        }),
        { token }
      );
      setItems(page.data);
      setMeta({
        limit: Number(page.meta?.limit || MEMBER_PAGE_SIZE),
        offset: Number(page.meta?.offset || 0),
        total: Number(page.meta?.total ?? page.data.length),
      });
    } catch (err) {
      setItems([]);
      setError(err.message || "Failed to load collection members.");
    } finally {
      setLoading(false);
    }
  }

  async function handleToggle() {
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded && items.length === 0 && !loading) {
      await loadMembers({ offset: 0, q: activeSearch });
    }
  }

  async function handleSearch(event) {
    event.preventDefault();
    const nextSearch = searchInput.trim();
    setActiveSearch(nextSearch);
    await loadMembers({ offset: 0, q: nextSearch });
  }

  async function handleDownload() {
    if (!snapshotId) {
      return;
    }
    setDownloadBusy(true);
    setError("");
    try {
      await downloadProtectedResource({
        path: `/collection-snapshots/${snapshotId}/manifest`,
        token,
        filename: manifestFilename || `collection-${snapshotId}-manifest.json`,
      });
    } catch (err) {
      setError(err.message || "Failed to download the collection manifest.");
    } finally {
      setDownloadBusy(false);
    }
  }

  const offset = Number(meta.offset || 0);
  const total = Number(meta.total || 0);
  const hasPrevious = offset > 0;
  const hasNext = offset + items.length < total;

  return (
    <div className="stack">
      <div className="inline">
        <button
          type="button"
          className="btn-secondary"
          disabled={!snapshotId || loading}
          aria-expanded={expanded}
          onClick={handleToggle}
        >
          {expanded ? "Hide members" : "Browse members"}
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={!snapshotId || downloadBusy}
          onClick={handleDownload}
        >
          {downloadBusy ? "Downloading..." : "Download manifest"}
        </button>
      </div>

      {error ? <p className="flash error">{error}</p> : null}

      {expanded ? (
        <div className="stack">
          <form className="inline" onSubmit={handleSearch}>
            <label>
              Member path search
              <input
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
                placeholder="trial-0001"
              />
            </label>
            <button type="submit" className="btn-secondary" disabled={loading}>
              Search
            </button>
          </form>

          {loading ? <p className="subtle">Loading up to 100 members...</p> : null}
          {!loading && items.length === 0 ? (
            <p className="subtle">
              {activeSearch ? "No members match this path." : "(no members)"}
            </p>
          ) : null}

          <div className="stack" aria-label="Collection members">
            {items.map((member) => (
              <div className="item" key={`${member.path}:${member.checksum}`}>
                <div className="item-head">
                  <span className="mono">{member.path}</span>
                  <span className="subtle">{formatBytes(member.size_bytes)}</span>
                </div>
                <p className="mono">sha256: {member.checksum}</p>
              </div>
            ))}
          </div>

          <div className="inline">
            <button
              type="button"
              className="btn-secondary"
              disabled={loading || !hasPrevious}
              onClick={() =>
                loadMembers({
                  offset: Math.max(0, offset - MEMBER_PAGE_SIZE),
                  q: activeSearch,
                })
              }
            >
              Previous members
            </button>
            <span className="subtle">
              {total === 0
                ? "0 members"
                : `${offset + 1}-${Math.min(offset + items.length, total)} of ${total}`}
            </span>
            <button
              type="button"
              className="btn-secondary"
              disabled={loading || !hasNext}
              onClick={() =>
                loadMembers({
                  offset: offset + MEMBER_PAGE_SIZE,
                  q: activeSearch,
                })
              }
            >
              Next members
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { CollectionSnapshotBrowser, MEMBER_PAGE_SIZE };
