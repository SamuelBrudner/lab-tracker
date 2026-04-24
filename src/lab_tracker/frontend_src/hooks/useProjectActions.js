import { apiRequest } from "../shared/api.js";

function useProjectActions({
  token,
  canWrite,
  refreshProjects,
  setBusy,
  setFlash,
  setSelectedProjectId,
  projectName,
  setProjectName,
  projectDescription,
  setProjectDescription,
}) {
  async function handleCreateProject(event) {
    event.preventDefault();
    if (!canWrite) {
      return;
    }
    if (!projectName.trim()) {
      setFlash("", "Project name is required.");
      return;
    }

    setBusy(true);
    setFlash("", "");
    try {
      const created = await apiRequest("/projects", {
        body: {
          description: projectDescription.trim() || null,
          name: projectName.trim(),
        },
        method: "POST",
        token,
      });
      setProjectName("");
      setProjectDescription("");
      await refreshProjects();
      setSelectedProjectId(created.project_id);
      setFlash("Project created.");
    } catch (err) {
      setFlash("", err.message || "Failed to create project.");
    } finally {
      setBusy(false);
    }
  }

  return {
    handleCreateProject,
  };
}

export { useProjectActions };
