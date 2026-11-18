import * as React from "react";

import { apiRequest } from "../../shared/api.js";
import { formatDate } from "../../shared/formatters.js";
import { AppLink } from "../../shared/routing.jsx";

const PURPOSES = {
  question: "Address a research question",
  exploratory: "Exploratory",
  calibration: "Calibration",
  maintenance: "Maintenance",
};

function WorkIntentEditor({ token, scope, workId, projectId, canWrite, navigate }) {
  const [open, setOpen] = React.useState(false);
  const [intent, setIntent] = React.useState(null);
  const [kind, setKind] = React.useState("");
  const [questionId, setQuestionId] = React.useState("");
  const [questions, setQuestions] = React.useState([]);
  const [busy, setBusy] = React.useState(false);
  const [loaded, setLoaded] = React.useState(false);
  const [error, setError] = React.useState("");
  const path = `/work-intent/${scope}/${workId}`;

  async function toggle() {
    setOpen(!open);
    if (open || loaded) return;
    setBusy(true);
    setError("");
    try {
      const { intent: saved } = await apiRequest(path, { token });
      setIntent(saved);
      setKind(saved?.kind || "");
      setQuestionId(saved?.question_id || "");
      if (canWrite) {
        const choices = await apiRequest(
          `/questions?project_id=${encodeURIComponent(projectId)}&limit=100`,
          { token },
        );
        setQuestions(choices.filter(({ status }) => status === "active" || status === "staged"));
      }
      setLoaded(true);
    } catch (err) {
      setError(err.message || "Work purpose could not be loaded.");
    } finally {
      setBusy(false);
    }
  }

  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const saved = await apiRequest(path, {
        token,
        method: "PUT",
        body: kind === "question" ? { kind, question_id: questionId } : { kind },
      });
      setIntent(saved);
    } catch (err) {
      setError(err.message || "Work purpose could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="stack">
      <button type="button" className="btn-secondary" onClick={toggle} aria-expanded={open}>
        Work purpose
      </button>
      {open ? (
        <>
          {error ? <p className="flash error" role="alert">{error}</p> : null}
          {intent ? (
            <p role="status">
              {intent.question_id ? (
                <AppLink to={`/app/questions/${intent.question_id}`} navigate={navigate}>
                  {intent.question_text}
                </AppLink>
              ) : (intent.question_text || PURPOSES[intent.kind])}
              <span className="subtle">
                {" · "}{intent.source === "confirmed_in_review" ? "Confirmed in review" : "Declared"}
                {" · "}{formatDate(intent.recorded_at)}
              </span>
            </p>
          ) : loaded ? <p className="subtle">Purpose hasn’t been recorded.</p> : null}
          {busy && !loaded ? <p role="status">Loading work purpose…</p> : null}
          {loaded && canWrite ? (
            <form className="stack" onSubmit={save}>
              <label>
                What was this work for?
                <select value={kind} onChange={(event) => setKind(event.target.value)} disabled={busy}>
                  <option value="">Choose a purpose</option>
                  {Object.entries(PURPOSES).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
              {kind === "question" ? (
                <label>
                  Intended question
                  <select
                    value={questionId}
                    onChange={(event) => setQuestionId(event.target.value)}
                    disabled={busy}
                  >
                    <option value="">Choose a question</option>
                    {questions.map((question) => (
                      <option value={question.question_id} key={question.question_id}>{question.text}</option>
                    ))}
                  </select>
                </label>
              ) : null}
              <button
                className="btn-primary"
                type="submit"
                disabled={busy || !kind || (kind === "question" && !questionId)}
              >
                Save work purpose
              </button>
            </form>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

export { WorkIntentEditor };
