import * as React from "react";

import { buildApiPath, fetchAllPages } from "../shared/api.js";

const { useCallback, useEffect, useMemo, useRef, useState } = React;

function useProjectWorkspaceData({ token, setBusy, setFlash }) {
  const [projects, setProjects] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [questions, setQuestions] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [notes, setNotes] = useState([]);
  const [sessions, setSessions] = useState([]);
  const projectsRequestRef = useRef(0);
  const projectDataRequestRef = useRef(0);

  const clearProjectState = useCallback(() => {
    setSelectedProjectId("");
    setQuestions([]);
    setDatasets([]);
    setNotes([]);
    setSessions([]);
  }, []);

  const refreshProjectData = useCallback(
    async (projectId) => {
      if (!projectId || !token) {
        return;
      }

      const requestId = projectDataRequestRef.current + 1;
      projectDataRequestRef.current = requestId;
      const [nextQuestions, nextDatasets, nextNotes, nextSessions] = await Promise.all([
        fetchAllPages(buildApiPath("/questions", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/datasets", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/notes", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/sessions", { project_id: projectId }), { token }),
      ]);

      if (projectDataRequestRef.current !== requestId) {
        return;
      }
      setQuestions(nextQuestions);
      setDatasets(nextDatasets);
      setNotes(nextNotes);
      setSessions(nextSessions);
    },
    [token]
  );

  const refreshProjects = useCallback(async () => {
    if (!token) {
      return [];
    }
    const requestId = projectsRequestRef.current + 1;
    projectsRequestRef.current = requestId;
    const nextProjects = await fetchAllPages("/projects", { token });
    if (projectsRequestRef.current !== requestId) {
      return nextProjects;
    }
    setProjects(nextProjects);
    if (nextProjects.length === 0) {
      clearProjectState();
      return nextProjects;
    }
    setSelectedProjectId((current) => {
      if (current && nextProjects.some((item) => item.project_id === current)) {
        return current;
      }
      return nextProjects[0].project_id;
    });
    return nextProjects;
  }, [clearProjectState, token]);

  useEffect(() => {
    if (!token) {
      projectsRequestRef.current += 1;
      projectDataRequestRef.current += 1;
      setProjects([]);
      clearProjectState();
      return;
    }

    let canceled = false;
    setBusy(true);
    refreshProjects()
      .catch((err) => {
        if (!canceled) {
          setFlash("", err.message || "Failed to load projects.");
        }
      })
      .finally(() => {
        if (!canceled) {
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [clearProjectState, refreshProjects, setBusy, setFlash, token]);

  useEffect(() => {
    if (!token || !selectedProjectId) {
      projectDataRequestRef.current += 1;
      setQuestions([]);
      setDatasets([]);
      setNotes([]);
      setSessions([]);
      return;
    }

    let canceled = false;
    setBusy(true);
    refreshProjectData(selectedProjectId)
      .catch((err) => {
        if (!canceled) {
          setFlash("", err.message || "Unable to load project data.");
        }
      })
      .finally(() => {
        if (!canceled) {
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [refreshProjectData, selectedProjectId, setBusy, setFlash, token]);

  const stagedQuestions = useMemo(
    () => questions.filter((item) => item.status === "staged"),
    [questions]
  );
  const activeQuestions = useMemo(
    () => questions.filter((item) => item.status === "active"),
    [questions]
  );
  const selectedProject = useMemo(
    () => projects.find((item) => item.project_id === selectedProjectId) || null,
    [projects, selectedProjectId]
  );

  return {
    activeQuestions,
    datasets,
    notes,
    projects,
    questions,
    refreshProjectData,
    refreshProjects,
    selectedProject,
    selectedProjectId,
    sessions,
    setSelectedProjectId,
    setSessions,
    stagedQuestions,
  };
}

export { useProjectWorkspaceData };
