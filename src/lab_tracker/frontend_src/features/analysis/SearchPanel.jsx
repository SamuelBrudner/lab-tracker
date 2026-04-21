import * as React from "react";

import { apiRequest } from "../../shared/api.js";
import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

const { useMemo, useState } = React;

function SearchPanel({ token, projects, selectedProjectId, navigate }) {
  const [query, setQuery] = useState("");
  const [projectFilter, setProjectFilter] = useState("__active__");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState({ questions: [], notes: [] });
  const [lastRanQuery, setLastRanQuery] = useState("");

  function relevanceLevel(rank) {
    if (rank <= 0) {
      return { label: "Top", className: "pill relevance relevance-top" };
    }
    if (rank <= 2) {
      return { label: "High", className: "pill relevance relevance-high" };
    }
    if (rank <= 6) {
      return { label: "Med", className: "pill relevance relevance-med" };
    }
    return { label: "Low", className: "pill relevance relevance-low" };
  }

  function resolveProjectId() {
    if (projectFilter === "__active__") {
      return selectedProjectId || "";
    }
    return projectFilter;
  }

  async function runSearch(event) {
    event.preventDefault();
    if (!token) {
      return;
    }
    const trimmed = query.trim();
    if (!trimmed) {
      setError("");
      setResults({ questions: [], notes: [] });
      setLastRanQuery("");
      return;
    }
    const resolvedProjectId = resolveProjectId();
    if (projectFilter === "__active__" && !resolvedProjectId) {
      setError("Select an active project, or switch the search filter to All projects.");
      setResults({ questions: [], notes: [] });
      setLastRanQuery(trimmed);
      return;
    }

    setBusy(true);
    setError("");
    try {
      const params = new URLSearchParams();
      params.set("q", trimmed);
      params.set("include", "questions,notes");
      params.set("limit", "20");
      if (resolvedProjectId) {
        params.set("project_id", resolvedProjectId);
      }
      const payload = await apiRequest(`/search?${params.toString()}`, { token });
      setResults(payload || { questions: [], notes: [] });
      setLastRanQuery(trimmed);
    } catch (err) {
      setError(err.message || "Search failed.");
    } finally {
      setBusy(false);
    }
  }

  const projectOptions = useMemo(() => {
    const options = [
      { value: "__active__", label: "Active project" },
      { value: "", label: "All projects" },
      ...projects.map((project) => ({
        label: project.name,
        value: project.project_id,
      })),
    ];
    const seen = new Set();
    return options.filter((item) => {
      if (seen.has(item.value)) {
        return false;
      }
      seen.add(item.value);
      return true;
    });
  }, [projects]);

  const questionHits = results.questions || [];
  const noteHits = results.notes || [];
  const hasResults = questionHits.length > 0 || noteHits.length > 0;
  const showEmptyState = lastRanQuery && !busy && !error && !hasResults;

  return (
    <article className="card span-12">
      <div className="item-head">
        <h2>Search</h2>
        {busy ? <span className="pill">Searching...</span> : null}
      </div>
      <p className="subtle">
        Queries the semantic search endpoint across questions and notes. Results are ordered by
        backend relevance.
      </p>

      <form className="form" onSubmit={runSearch}>
        <div className="search-row">
          <label className="search-input">
            Query
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="e.g. photometry artifact, opto stimulation, trial alignment"
              disabled={!token}
            />
          </label>

          <label className="search-filter">
            Project filter
            <select
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
              disabled={!token}
            >
              {projectOptions.map((option) => (
                <option key={option.value || "__all__"} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="inline">
          <button className="btn-primary" disabled={!token || busy || !query.trim()}>
            Search
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={!token || busy || (!query && !hasResults && !error)}
            onClick={() => {
              setQuery("");
              setError("");
              setResults({ questions: [], notes: [] });
              setLastRanQuery("");
            }}
          >
            Clear
          </button>
        </div>
      </form>

      {error ? <p className="flash error">{error}</p> : null}
      {showEmptyState ? <p className="subtle">No hits for &quot;{lastRanQuery}&quot;.</p> : null}

      {hasResults ? (
        <div className="grid search-results">
          <section className="card span-6">
            <div className="item-head">
              <h3>Questions</h3>
              <span className="pill">{questionHits.length}</span>
            </div>
            {questionHits.length === 0 ? (
              <p className="subtle">No matching questions.</p>
            ) : (
              <div className="stack">
                {questionHits.map((question, index) => {
                  const relevance = relevanceLevel(index);
                  return (
                    <article className="item" key={question.question_id}>
                      <div className="item-head">
                        <AppLink
                          to={`/app/questions/${question.question_id}`}
                          navigate={navigate}
                          className="link"
                        >
                          <strong>{question.text}</strong>
                        </AppLink>
                        <span className={relevance.className} title={`${relevance.label} relevance`}>
                          #{index + 1}
                        </span>
                      </div>
                      <div className="inline">
                        <span className="pill">{question.status}</span>
                        <span className="pill">{question.question_type}</span>
                      </div>
                      {question.hypothesis ? (
                        <p className="subtle">Hypothesis: {question.hypothesis}</p>
                      ) : null}
                      <p className="mono">{question.question_id}</p>
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <section className="card span-6">
            <div className="item-head">
              <h3>Notes</h3>
              <span className="pill">{noteHits.length}</span>
            </div>
            {noteHits.length === 0 ? (
              <p className="subtle">No matching notes.</p>
            ) : (
              <div className="stack">
                {noteHits.map((note, index) => {
                  const relevance = relevanceLevel(index);
                  const preview = note.transcribed_text || note.raw_content || "(binary upload)";
                  return (
                    <article className="item" key={note.note_id}>
                      <div className="item-head">
                        <AppLink
                          to={`/app/notes/${note.note_id}`}
                          navigate={navigate}
                          className="link"
                        >
                          <strong>{preview.slice(0, 110)}</strong>
                        </AppLink>
                        <span className={relevance.className} title={`${relevance.label} relevance`}>
                          #{index + 1}
                        </span>
                      </div>
                      <div className="inline">
                        <span className="pill">{note.status}</span>
                        <span className="subtle">{formatDate(note.created_at)}</span>
                      </div>
                      <p className="mono">{note.note_id}</p>
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      ) : null}
    </article>
  );
}

export { SearchPanel };
