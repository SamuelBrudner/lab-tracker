import * as React from "react";

import { apiListRequest, buildApiPath, fetchAllPages } from "../shared/api.js";

const { useCallback, useEffect, useMemo, useRef, useState } = React;

function useProjectWorkspaceData({ token, setBusy, setFlash, loadProjectData = true }) {
  const [projects, setProjects] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [questions, setQuestions] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [projectCounts, setProjectCounts] = useState({
    datasets: 0,
    notes: 0,
    questions: 0,
  });
  const projectsRequestRef = useRef(0);
  const projectDataRequestRef = useRef(0);

  const clearProjectState = useCallback(() => {
    setSelectedProjectId("");
    setQuestions([]);
    setDatasets([]);
    setProjectCounts({ datasets: 0, notes: 0, questions: 0 });
  }, []);

  const refreshProjectData = useCallback(
    async (projectId) => {
      if (!projectId || !token) {
        return;
      }

      const requestId = projectDataRequestRef.current + 1;
      projectDataRequestRef.current = requestId;
      const [nextQuestions, nextDatasets, notePage] = await Promise.all([
        fetchAllPages(buildApiPath("/questions", { project_id: projectId }), { token }),
        fetchAllPages(buildApiPath("/datasets", { project_id: projectId }), { token }),
        apiListRequest(buildApiPath("/notes", { project_id: projectId, limit: 1, offset: 0 }), {
          token,
        }),
      ]);

      if (projectDataRequestRef.current !== requestId) {
        return;
      }
      setQuestions(nextQuestions);
      setDatasets(nextDatasets);
      setProjectCounts({
        datasets: nextDatasets.length,
        notes: notePage.meta.total,
        questions: nextQuestions.length,
      });
    },
    [token]
  );

  const refreshProjectCounts = useCallback(
    async (projectId, { clearCollections = false } = {}) => {
      if (!projectId || !token) {
        return;
      }

      const requestId = projectDataRequestRef.current + 1;
      projectDataRequestRef.current = requestId;
      const [questionPage, datasetPage, notePage] = await Promise.all([
        apiListRequest(buildApiPath("/questions", { project_id: projectId, limit: 1, offset: 0 }), {
          token,
        }),
        apiListRequest(buildApiPath("/datasets", { project_id: projectId, limit: 1, offset: 0 }), {
          token,
        }),
        apiListRequest(buildApiPath("/notes", { project_id: projectId, limit: 1, offset: 0 }), {
          token,
        }),
      ]);

      if (projectDataRequestRef.current !== requestId) {
        return;
      }
      if (clearCollections) {
        setQuestions([]);
        setDatasets([]);
      }
      setProjectCounts({
        datasets: datasetPage.meta.total,
        notes: notePage.meta.total,
        questions: questionPage.meta.total,
      });
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
      setProjectCounts({ datasets: 0, notes: 0, questions: 0 });
      return;
    }

    let canceled = false;
    setBusy(true);
    const loader = loadProjectData
      ? refreshProjectData
      : (projectId) => refreshProjectCounts(projectId, { clearCollections: true });
    loader(selectedProjectId)
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
  }, [
    loadProjectData,
    refreshProjectCounts,
    refreshProjectData,
    selectedProjectId,
    setBusy,
    setFlash,
    token,
  ]);

  const stagedQuestions = useMemo(
    () => questions.filter((item) => item.status === "staged"),
    [questions]
  );
  const activeQuestions = useMemo(
    () => questions.filter((item) => item.status === "active"),
    [questions]
  );
  const stagedDatasets = useMemo(
    () => datasets.filter((item) => item.status === "staged"),
    [datasets]
  );
  const selectedProject = useMemo(
    () => projects.find((item) => item.project_id === selectedProjectId) || null,
    [projects, selectedProjectId]
  );

  return {
    activeQuestions,
    datasets,
    noteCount: projectCounts.notes,
    projects,
    questionCount: projectCounts.questions,
    questions,
    refreshProjectData,
    refreshProjectCounts,
    refreshProjects,
    selectedProject,
    selectedProjectId,
    setSelectedProjectId,
    stagedDatasets,
    stagedQuestions,
    datasetCount: projectCounts.datasets,
  };
}

export { useProjectWorkspaceData };
