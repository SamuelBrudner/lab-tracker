import * as React from "react";

function Dashboard({
  projects,
  questions,
  datasets,
  notes,
  selectedProjectId,
  onSelectedProjectChange,
  canWrite,
  busy,
  projectName,
  projectDescription,
  onProjectNameChange,
  onProjectDescriptionChange,
  onCreateProject,
}) {
  return (
    <article className="card span-4">
      <h2>Dashboard</h2>
      <div className="inline">
        <div className="kpi">
          <span className="subtle">Projects</span>
          <strong>{projects.length}</strong>
        </div>
        <div className="kpi">
          <span className="subtle">Questions</span>
          <strong>{questions.length}</strong>
        </div>
        <div className="kpi">
          <span className="subtle">Datasets</span>
          <strong>{datasets.length}</strong>
        </div>
        <div className="kpi">
          <span className="subtle">Notes</span>
          <strong>{notes.length}</strong>
        </div>
      </div>

      <label>
        Active project
        <select value={selectedProjectId} onChange={onSelectedProjectChange}>
          <option value="">Select a project</option>
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>

      <form className="form" onSubmit={onCreateProject}>
        <h3>New Project</h3>
        <label>
          Name
          <input value={projectName} onChange={onProjectNameChange} disabled={!canWrite} />
        </label>
        <label>
          Description
          <textarea
            value={projectDescription}
            onChange={onProjectDescriptionChange}
            disabled={!canWrite}
          />
        </label>
        <button className="btn-primary" disabled={!canWrite || busy}>
          Create project
        </button>
      </form>

      {!canWrite ? (
        <p className="warn">
          Your role is read-only. Ask an admin to provision an editor or admin account for write
          workflows.
        </p>
      ) : null}
    </article>
  );
}

export { Dashboard };
