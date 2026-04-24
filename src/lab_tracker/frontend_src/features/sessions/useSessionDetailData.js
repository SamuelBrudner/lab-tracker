import * as React from "react";

import { buildApiPath, fetchAllPages } from "../../shared/api.js";
import { useApiResource } from "../../hooks/useApiResource.js";

const { useEffect, useMemo, useState } = React;

function useSessionDetailData({ token, sessionId, projects }) {
  const { data: session, error: loadError, loading } = useApiResource(
    token && sessionId ? `/sessions/${sessionId}` : "",
    token,
    "Failed to load session."
  );
  const [outputsState, setOutputsState] = useState({ loading: false, error: "", items: [] });
  const [noteState, setNoteState] = useState({ loading: false, error: "", items: [] });
  const [activeQuestionState, setActiveQuestionState] = useState({
    loading: false,
    error: "",
    items: [],
  });
  const { data: primaryQuestionData } = useApiResource(
    token && session?.primary_question_id ? `/questions/${session.primary_question_id}` : "",
    token,
    "Failed to load primary question."
  );

  const project = useMemo(() => {
    if (!session) {
      return null;
    }
    return projects.find((item) => item.project_id === session.project_id) || null;
  }, [projects, session]);

  const primaryQuestion = useMemo(() => {
    if (!session?.primary_question_id) {
      return null;
    }
    return (
      activeQuestionState.items.find((item) => item.question_id === session.primary_question_id) ||
      primaryQuestionData ||
      null
    );
  }, [activeQuestionState.items, primaryQuestionData, session]);

  useEffect(() => {
    let canceled = false;
    if (!token || !sessionId) {
      setOutputsState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setOutputsState({ loading: true, error: "", items: [] });
    fetchAllPages(`/sessions/${sessionId}/outputs`, { token })
      .then((items) => {
        if (canceled) {
          return;
        }
        const normalized = Array.isArray(items) ? items : [];
        normalized.sort((a, b) => {
          const aTime = Date.parse(a.created_at || "") || 0;
          const bTime = Date.parse(b.created_at || "") || 0;
          return bTime - aTime;
        });
        setOutputsState({ loading: false, error: "", items: normalized });
      })
      .catch((err) => {
        if (!canceled) {
          setOutputsState({
            loading: false,
            error: err.message || "Failed to load outputs.",
            items: [],
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [sessionId, token]);

  useEffect(() => {
    let canceled = false;
    if (!token || !session) {
      setActiveQuestionState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setActiveQuestionState({ loading: true, error: "", items: [] });
    fetchAllPages(
      buildApiPath("/questions", {
        project_id: session.project_id,
        status: "active",
      }),
      { token }
    )
      .then((items) => {
        if (canceled) {
          return;
        }
        setActiveQuestionState({
          loading: false,
          error: "",
          items: Array.isArray(items) ? items : [],
        });
      })
      .catch((err) => {
        if (!canceled) {
          setActiveQuestionState({
            loading: false,
            error: err.message || "Failed to load active questions.",
            items: [],
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [session, token]);

  useEffect(() => {
    let canceled = false;
    if (!token || !session) {
      setNoteState({ loading: false, error: "", items: [] });
      return () => {
        canceled = true;
      };
    }

    setNoteState({ loading: true, error: "", items: [] });
    fetchAllPages(
      buildApiPath("/notes", {
        project_id: session.project_id,
        target_entity_type: "session",
        target_entity_id: session.session_id,
      }),
      { token }
    )
      .then((items) => {
        if (canceled) {
          return;
        }
        const normalized = Array.isArray(items) ? items : [];
        normalized.sort((a, b) => {
          const aTime = Date.parse(a.created_at || "") || 0;
          const bTime = Date.parse(b.created_at || "") || 0;
          return bTime - aTime;
        });
        setNoteState({ loading: false, error: "", items: normalized });
      })
      .catch((err) => {
        if (!canceled) {
          setNoteState({
            loading: false,
            error: err.message || "Failed to load linked notes.",
            items: [],
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [session, token]);

  return {
    activeQuestionState,
    loadError,
    loading,
    noteState,
    outputsState,
    primaryQuestion,
    project,
    session,
  };
}

export { useSessionDetailData };
