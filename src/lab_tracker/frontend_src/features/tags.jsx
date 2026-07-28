import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";

const { useCallback, useEffect, useState } = React;

const TAG_MODE_OPTIONS = [
  { label: "Standard capture", value: "" },
  { label: "Voice-first", value: "voice" },
];

function sessionOptionLabel(session) {
  const type = session.session_type === "scientific" ? "Scientific" : "Operational";
  return `${type} · ${session.link_code || session.session_id}`;
}

function BenchTagsCard({ token, canWrite, setFlash }) {
  const [projects, setProjects] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [bindings, setBindings] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [label, setLabel] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [hint, setHint] = useState("");
  const [mode, setMode] = useState("");
  const [logBreadcrumb, setLogBreadcrumb] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busySlug, setBusySlug] = useState("");
  const [repointTargets, setRepointTargets] = useState({});
  const [printItems, setPrintItems] = useState(null);
  const [printing, setPrinting] = useState(false);

  useEffect(() => {
    let canceled = false;
    apiListRequest(buildApiPath("/projects", { limit: 200 }), { token })
      .then(({ data }) => {
        if (canceled) {
          return;
        }
        setProjects(data || []);
        setSelectedProjectId((current) => current || data?.[0]?.project_id || "");
      })
      .catch((err) => {
        if (!canceled) {
          setFlash("", err.message || "Failed to load projects for bench tags.");
        }
      });
    return () => {
      canceled = true;
    };
  }, [setFlash, token]);

  const refreshBindings = useCallback(async () => {
    if (!selectedProjectId) {
      setBindings([]);
      return;
    }
    setLoading(true);
    try {
      const page = await apiListRequest(
        buildApiPath("/tags", { project_id: selectedProjectId, limit: 200 }),
        { token }
      );
      setBindings(page.data || []);
    } catch (err) {
      setFlash("", err.message || "Failed to load bench tags.");
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, setFlash, token]);

  useEffect(() => {
    refreshBindings();
  }, [refreshBindings]);

  useEffect(() => {
    let canceled = false;
    setSessions([]);
    setSessionId("");
    setPrintItems(null);
    if (!selectedProjectId) {
      return () => {
        canceled = true;
      };
    }
    apiListRequest(
      buildApiPath("/sessions", { project_id: selectedProjectId, limit: 200 }),
      { token }
    )
      .then(({ data }) => {
        if (!canceled) {
          setSessions(data || []);
        }
      })
      .catch(() => {
        // The session selector is optional; tags bind fine without one.
        if (!canceled) {
          setSessions([]);
        }
      });
    return () => {
      canceled = true;
    };
  }, [selectedProjectId, token]);

  useEffect(() => {
    if (printItems && printItems.length > 0 && typeof window !== "undefined") {
      window.print();
    }
  }, [printItems]);

  async function handleCreate(event) {
    event.preventDefault();
    setCreating(true);
    setFlash("", "");
    try {
      const body = {
        label: label.trim(),
        project_id: selectedProjectId,
        log_breadcrumb: logBreadcrumb,
      };
      if (sessionId) {
        body.session_id = sessionId;
      }
      if (hint.trim()) {
        body.hint = hint.trim();
      }
      if (mode) {
        body.mode = mode;
      }
      const created = await apiRequest("/tags", { body, method: "POST", token });
      // The cached print sheet no longer reflects the bindings; drop it so
      // printing never uses stale data.
      setPrintItems(null);
      setFlash(`Tag "${created.label}" bound to /t/${created.slug}.`);
      setLabel("");
      setSessionId("");
      setHint("");
      setMode("");
      setLogBreadcrumb(false);
      await refreshBindings();
    } catch (err) {
      setFlash("", err.message || "Failed to create tag.");
    } finally {
      setCreating(false);
    }
  }

  async function handleRepoint(slug) {
    const targetProjectId = repointTargets[slug];
    if (!targetProjectId || targetProjectId === selectedProjectId) {
      return;
    }
    setBusySlug(slug);
    setFlash("", "");
    try {
      await apiRequest(`/tags/${slug}`, {
        body: { project_id: targetProjectId },
        method: "PATCH",
        token,
      });
      // Invalidate the cached print sheet: the binding just changed.
      setPrintItems(null);
      setFlash(`Tag /t/${slug} re-pointed — the printed sticker keeps working.`);
      setRepointTargets((current) => {
        const next = { ...current };
        delete next[slug];
        return next;
      });
      await refreshBindings();
    } catch (err) {
      setFlash("", err.message || "Failed to re-point tag.");
    } finally {
      setBusySlug("");
    }
  }

  async function handleDelete(slug) {
    setBusySlug(slug);
    setFlash("", "");
    try {
      await apiRequest(`/tags/${slug}`, { method: "DELETE", token });
      // Invalidate the cached print sheet: it still lists the deleted tag.
      setPrintItems(null);
      setFlash(`Tag /t/${slug} deleted.`);
      await refreshBindings();
    } catch (err) {
      setFlash("", err.message || "Failed to delete tag.");
    } finally {
      setBusySlug("");
    }
  }

  async function handlePrint() {
    setPrinting(true);
    setFlash("", "");
    try {
      const page = await apiListRequest(
        buildApiPath("/tags/print", { project_id: selectedProjectId, limit: 200 }),
        { token }
      );
      if (!page.data.length) {
        setPrintItems(null);
        setFlash("", "No tags to print for this project yet.");
        return;
      }
      setPrintItems(page.data);
    } catch (err) {
      setFlash("", err.message || "Failed to build the print sheet.");
    } finally {
      setPrinting(false);
    }
  }

  return (
    <section className="card-inset bench-tags">
      <h3>Bench tags</h3>
      <p className="subtle">
        Print QR stickers that open phone capture with a project pre-filled.
        Tags are always optional — capture works exactly the same without
        them — and re-pointing a tag never needs a reprint.
      </p>

      <div className="form">
        <label>
          Project
          <select
            value={selectedProjectId}
            onChange={(event) => setSelectedProjectId(event.target.value)}
          >
            <option value="">Select a project</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <form className="form" onSubmit={handleCreate}>
        <label>
          Tag label
          <input
            type="text"
            value={label}
            placeholder="Bench 3 rig"
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <label>
          Session (optional)
          <select
            value={sessionId}
            onChange={(event) => setSessionId(event.target.value)}
            disabled={sessions.length === 0}
          >
            <option value="">No session</option>
            {sessions.map((session) => (
              <option key={session.session_id} value={session.session_id}>
                {sessionOptionLabel(session)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Hint (optional)
          <input
            type="text"
            value={hint}
            placeholder="Prefilled short hint"
            onChange={(event) => setHint(event.target.value)}
          />
        </label>
        <label>
          Capture mode
          <select value={mode} onChange={(event) => setMode(event.target.value)}>
            {TAG_MODE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={logBreadcrumb}
            onChange={(event) => setLogBreadcrumb(event.target.checked)}
          />
          Log a breadcrumb note on tap
        </label>
        <button
          type="submit"
          className="btn-primary"
          disabled={!canWrite || !selectedProjectId || !label.trim() || creating}
        >
          {creating ? "Creating tag…" : "Create tag"}
        </button>
      </form>

      {loading ? (
        <p className="subtle">Loading bench tags…</p>
      ) : bindings.length === 0 ? (
        <p className="subtle">No tags bound to this project yet.</p>
      ) : (
        <ul className="list-clean">
          {bindings.map((binding) => (
            <li key={binding.slug} className="row-between">
              <div>
                <strong>{binding.label}</strong>
                <div className="subtle">
                  <span className="mono">/t/{binding.slug}</span>
                  {binding.session_id ? " · session-bound" : ""}
                  {binding.mode ? ` · ${binding.mode}` : ""}
                  {binding.hint ? ` · hint: ${binding.hint}` : ""}
                  {binding.log_breadcrumb ? " · breadcrumb" : ""}
                </div>
              </div>
              <div className="inline">
                <label>
                  Re-point to
                  <select
                    value={repointTargets[binding.slug] || ""}
                    onChange={(event) =>
                      setRepointTargets((current) => ({
                        ...current,
                        [binding.slug]: event.target.value,
                      }))
                    }
                  >
                    <option value="">Choose project</option>
                    {projects
                      .filter((project) => project.project_id !== selectedProjectId)
                      .map((project) => (
                        <option key={project.project_id} value={project.project_id}>
                          {project.name}
                        </option>
                      ))}
                  </select>
                </label>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={
                    !canWrite ||
                    busySlug === binding.slug ||
                    !repointTargets[binding.slug]
                  }
                  onClick={() => handleRepoint(binding.slug)}
                >
                  {busySlug === binding.slug ? "Working…" : "Re-point"}
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={!canWrite || busySlug === binding.slug}
                  onClick={() => handleDelete(binding.slug)}
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="inline">
        <button
          type="button"
          className="btn-secondary"
          disabled={!selectedProjectId || bindings.length === 0 || printing}
          onClick={handlePrint}
        >
          {printing ? "Preparing sheet…" : "Print sheet"}
        </button>
        {printItems ? (
          <button type="button" className="btn-link" onClick={() => setPrintItems(null)}>
            Hide print sheet
          </button>
        ) : null}
      </div>

      {printItems && printItems.length > 0 ? (
        <div className="tag-print-sheet">
          {printItems.map((item) => (
            <figure className="tag-print-item" key={item.slug}>
              <div
                className="tag-print-qr"
                aria-label={`QR code for ${item.label}`}
                /* segno returns an inline <svg> from our own API; the server
                   already controls what URL it encodes, so trusted insertion
                   is fine (same pattern as the enrollment QR). */
                dangerouslySetInnerHTML={{ __html: item.qr_svg }}
              />
              <figcaption>
                <strong>{item.label}</strong>
                <div className="mono">{item.short_url}</div>
              </figcaption>
            </figure>
          ))}
        </div>
      ) : null}
    </section>
  );
}

export { BenchTagsCard };
