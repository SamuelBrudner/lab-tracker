import * as React from "react";

function Dashboard({
  projects,
  questionCount,
  datasetCount,
  noteCount,
  selectedProjectId,
  onSelectedProjectChange,
  canWrite,
  busy,
  projectName,
  projectDescription,
  onProjectNameChange,
  onProjectDescriptionChange,
  onCreateProject,
  onOpenGraph = () => {},
  projectMembers = [],
  canManageProjectMembers = false,
  memberUsername = "",
  memberRole = "contributor",
  onMemberUsernameChange = () => {},
  onMemberRoleChange = () => {},
  onAddProjectMember = () => {},
  onUpdateProjectMember = () => {},
  onRemoveProjectMember = () => {},
}) {
  return (
    <article className="card span-4 dashboard-card">
      <h2>Dashboard</h2>
      <div className="inline">
        <div className="kpi">
          <span className="subtle">Projects</span>
          <strong>{projects.length}</strong>
        </div>
        <div className="kpi">
          <span className="subtle">Questions</span>
          <strong>{questionCount}</strong>
        </div>
        <div className="kpi">
          <span className="subtle">Datasets</span>
          <strong>{datasetCount}</strong>
        </div>
        <div className="kpi">
          <span className="subtle">Notes</span>
          <strong>{noteCount}</strong>
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
      <button
        type="button"
        className="btn-secondary"
        disabled={!selectedProjectId}
        onClick={onOpenGraph}
      >
        Open graph
      </button>

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

      <section className="form">
        <h3>Project Members</h3>
        {projectMembers.length > 0 ? (
          <div className="stack">
            {projectMembers.map((member) => (
              <article className="item" key={member.membership_id}>
                <div className="item-head">
                  <strong>{member.username || member.user_id}</strong>
                  <span className="pill">{member.role}</span>
                </div>
                <div className="inline">
                  <select
                    value={member.role}
                    disabled={!canManageProjectMembers || busy}
                    onChange={(event) => onUpdateProjectMember(member.user_id, event.target.value)}
                  >
                    <option value="viewer">viewer</option>
                    <option value="contributor">contributor</option>
                    <option value="owner">owner</option>
                  </select>
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={!canManageProjectMembers || busy}
                    onClick={() => onRemoveProjectMember(member.user_id)}
                  >
                    Remove
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="subtle">No project members listed.</p>
        )}
        {canManageProjectMembers ? (
          <form className="inline" onSubmit={onAddProjectMember}>
            <label>
              Username
              <input value={memberUsername} onChange={onMemberUsernameChange} />
            </label>
            <label>
              Role
              <select value={memberRole} onChange={onMemberRoleChange}>
                <option value="viewer">viewer</option>
                <option value="contributor">contributor</option>
                <option value="owner">owner</option>
              </select>
            </label>
            <button className="btn-primary" disabled={busy || !selectedProjectId}>
              Add member
            </button>
          </form>
        ) : null}
      </section>
    </article>
  );
}

export { Dashboard };
