import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { formatDate } from "../shared/formatters.js";

const { useCallback, useEffect, useRef, useState } = React;

// The project owner's grant: what the drafting pass and Curate graph
// (delegated) tokens may apply on their own. Values are the server's
// DelegatedCurationPolicy enum.
const DELEGATED_CURATION_OPTIONS = [
  {
    value: "off",
    label: "Off — a person reviews every proposal",
    detail: "Every AI proposal waits in the review queue until someone accepts and commits it.",
  },
  {
    value: "organize",
    label: "Organize only — link captures into the graph",
    detail:
      "AI may apply links from notes to questions, sessions, datasets, and analyses, and " +
      "from records to goals. New records, retirements, question closures, and claim " +
      "resolutions still wait for a person.",
  },
  {
    value: "full",
    label: "Everything — apply every valid proposal",
    detail:
      "AI may apply every proposal it drafts, including new records, retirements, " +
      "question closures, and claim resolutions. Clarification requests always wait " +
      "for a person.",
  },
];
const GRANT_CONSENT_TEXT =
  "I understand that AI will change this project's graph without anyone reviewing those changes, and that each one is recorded as auto-accepted under my grant.";

function optionFor(value) {
  return DELEGATED_CURATION_OPTIONS.find((option) => option.value === value) || null;
}

function rank(value) {
  return DELEGATED_CURATION_OPTIONS.findIndex((option) => option.value === value);
}

function DelegatedCurationForm({ token, projectId, setBusy, setFlash }) {
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(false);
  const [policy, setPolicy] = useState("off");
  const [consent, setConsent] = useState(false);
  // Each load bumps the generation; a response from an older load (for a
  // previously selected project) must never populate this form.
  const loadGenerationRef = useRef(0);
  const currentProjectIdRef = useRef(projectId);

  useEffect(() => {
    currentProjectIdRef.current = projectId;
    return () => {
      currentProjectIdRef.current = null;
    };
  }, [projectId]);

  const loadSettings = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    const isCurrent = () => generation === loadGenerationRef.current;
    setSettings(null);
    setPolicy("off");
    setConsent(false);
    if (!projectId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const next = await apiRequest(
        `/projects/${projectId}/graph-draft-batch-settings/project-default`,
        { token }
      );
      if (!isCurrent()) {
        return;
      }
      setSettings(next);
      setPolicy(next.delegated_curation || "off");
    } catch (err) {
      if (isCurrent()) {
        setFlash("", err.message || "Failed to load delegated curation.");
      }
    } finally {
      if (isCurrent()) {
        setLoading(false);
      }
    }
  }, [projectId, setFlash, token]);

  useEffect(() => {
    loadSettings();
    return () => {
      loadGenerationRef.current += 1;
    };
  }, [loadSettings]);

  const stored = settings?.delegated_curation || "off";
  // Widening the grant (off -> organize -> full, or organize <-> full) is a
  // fresh act of consent every time; narrowing it never needs one.
  const widening = Boolean(settings) && policy !== stored && policy !== "off";
  const disabled = loading || !projectId || !settings;

  async function save(event) {
    event.preventDefault();
    if (disabled || policy === stored) {
      return;
    }
    if (widening && !consent) {
      setFlash("", "Tick the acknowledgement to widen delegated curation.");
      return;
    }
    const savedProjectId = projectId;
    setBusy(true);
    setFlash("", "");
    try {
      const next = await apiRequest(
        `/projects/${projectId}/graph-draft-batch-settings/project-default`,
        {
          body: {
            delegated_curation: policy,
            ...(widening ? { delegated_curation_acknowledged: true } : {}),
          },
          method: "PATCH",
          token,
        }
      );
      if (currentProjectIdRef.current !== savedProjectId) {
        setFlash("Delegated curation saved for the project you were editing.");
        return;
      }
      setSettings(next);
      setPolicy(next.delegated_curation || "off");
      setConsent(false);
      setFlash(
        next.delegated_curation === "off"
          ? "Delegated curation is off; every proposal waits for a person again."
          : `Delegated curation set to "${next.delegated_curation}".`
      );
    } catch (err) {
      setFlash("", err.message || "Failed to update delegated curation.");
    } finally {
      setBusy(false);
    }
  }

  const selected = optionFor(policy);

  return (
    <form className="form" onSubmit={save} aria-label="Delegated curation settings">
      <label>
        Delegated curation
        <select
          value={policy}
          disabled={disabled}
          onChange={(event) => {
            setPolicy(event.target.value);
            setConsent(false);
          }}
        >
          {DELEGATED_CURATION_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      {selected ? <p className="subtle">{selected.detail}</p> : null}
      <p className="subtle">
        Applies to the daily review, to drafts requested from a capture, and to agents
        connected with a Curate graph (delegated) token. Editing, rejecting, and deferring
        proposals stay yours.
      </p>
      {widening ? (
        <label className="inline toggle-row">
          <input
            type="checkbox"
            checked={consent}
            disabled={disabled}
            onChange={(event) => setConsent(event.target.checked)}
          />
          {GRANT_CONSENT_TEXT}
        </label>
      ) : null}
      {settings?.delegated_curation_granted_at ? (
        <p className="subtle">
          Granted {formatDate(settings.delegated_curation_granted_at)}
          {rank(stored) > 0 ? ` (${stored})` : ""}
        </p>
      ) : null}
      <div className="inline">
        <button
          className="btn-primary"
          disabled={disabled || policy === stored || (widening && !consent)}
        >
          {loading ? "Loading…" : "Save delegated curation"}
        </button>
      </div>
    </form>
  );
}

export { DELEGATED_CURATION_OPTIONS, DelegatedCurationForm };
