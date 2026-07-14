import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";

function UsersPage({ token, canManageUsers, projects = [], setBusy, setFlash }) {
  const [inviteEmail, setInviteEmail] = React.useState("");
  const [inviteRole, setInviteRole] = React.useState("editor");
  const [inviteProjectId, setInviteProjectId] = React.useState("");
  const [inviteProjectRole, setInviteProjectRole] = React.useState("contributor");
  const [inviteReviewEnabled, setInviteReviewEnabled] = React.useState(false);
  const [inviteReviewTime, setInviteReviewTime] = React.useState("17:00");
  const [inviteReviewTimezone, setInviteReviewTimezone] = React.useState(
    "America/New_York"
  );
  const [invitations, setInvitations] = React.useState([]);
  const [latestInvitation, setLatestInvitation] = React.useState(null);
  const [users, setUsers] = React.useState([]);
  const [passwordsByUser, setPasswordsByUser] = React.useState({});

  const refreshUsers = React.useCallback(async () => {
    if (!canManageUsers) {
      setUsers([]);
      return;
    }
    try {
      const { data } = await apiListRequest(buildApiPath("/auth/users", { limit: 200 }), {
        token,
      });
      setUsers(data);
    } catch (err) {
      setUsers([]);
      setFlash("", err.message || "Failed to load users.");
    }
  }, [canManageUsers, setFlash, token]);

  const refreshInvitations = React.useCallback(async () => {
    if (!canManageUsers) {
      setInvitations([]);
      return;
    }
    try {
      const { data } = await apiListRequest(buildApiPath("/auth/invitations", { limit: 200 }), {
        token,
      });
      setInvitations(data);
    } catch (err) {
      setInvitations([]);
      setFlash("", err.message || "Failed to load invitations.");
    }
  }, [canManageUsers, setFlash, token]);

  React.useEffect(() => {
    refreshUsers();
    refreshInvitations();
  }, [refreshInvitations, refreshUsers]);

  async function updateUser(userId, body, successMessage) {
    if (!canManageUsers) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/auth/users/${userId}`, {
        body,
        method: "PATCH",
        token,
      });
      await refreshUsers();
      setFlash(successMessage);
    } catch (err) {
      setFlash("", err.message || "Failed to update user.");
    } finally {
      setBusy(false);
    }
  }

  async function createInvitation(event) {
    event.preventDefault();
    if (!canManageUsers || !inviteEmail.trim()) {
      setFlash("", "Invite email is required.");
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      const invitation = await apiRequest("/auth/invitations", {
        body: {
          email: inviteEmail.trim(),
          role: inviteRole,
          ...(inviteProjectId
            ? {
                project_id: inviteProjectId,
                project_role: inviteProjectRole,
                review_enabled: inviteReviewEnabled,
                ...(inviteReviewEnabled
                  ? {
                      review_cadence_minutes: 1440,
                      review_run_at_local_time: inviteReviewTime,
                      review_timezone_name: inviteReviewTimezone,
                    }
                  : {}),
              }
            : {}),
        },
        method: "POST",
        token,
      });
      setLatestInvitation(invitation);
      setInviteEmail("");
      await refreshInvitations();
      setFlash("Invitation link created.");
    } catch (err) {
      setFlash("", err.message || "Failed to create invitation.");
    } finally {
      setBusy(false);
    }
  }

  async function revokeInvitation(invitationId) {
    if (!canManageUsers) {
      return;
    }
    setBusy(true);
    setFlash("", "");
    try {
      await apiRequest(`/auth/invitations/${invitationId}`, {
        method: "DELETE",
        token,
      });
      setLatestInvitation((current) =>
        current?.invitation_id === invitationId ? null : current
      );
      await refreshInvitations();
      setFlash("Invitation revoked.");
    } catch (err) {
      setFlash("", err.message || "Failed to revoke invitation.");
    } finally {
      setBusy(false);
    }
  }

  function updatePasswordDraft(userId, value) {
    setPasswordsByUser((current) => ({ ...current, [userId]: value }));
  }

  async function resetPassword(event, userId) {
    event.preventDefault();
    const password = String(passwordsByUser[userId] || "");
    if (!password) {
      setFlash("", "Password is required.");
      return;
    }
    await updateUser(userId, { password }, "Password reset.");
    updatePasswordDraft(userId, "");
  }

  if (!canManageUsers) {
    return (
      <article className="card span-8">
        <h2>Users</h2>
        <p className="warn">Admin privileges are required.</p>
      </article>
    );
  }

  return (
    <article className="card span-8">
      <h2>Users</h2>
      <section className="form invite-panel">
        <h3>Invite by Email</h3>
        <form className="inline" onSubmit={createInvitation}>
          <label>
            Email
            <input
              type="email"
              value={inviteEmail}
              onChange={(event) => setInviteEmail(event.target.value)}
              autoComplete="email"
            />
          </label>
          <label>
            Global role
            <select value={inviteRole} onChange={(event) => setInviteRole(event.target.value)}>
              <option value="editor">editor</option>
              <option value="viewer">viewer</option>
              <option value="admin">admin</option>
            </select>
          </label>
          <label>
            Project
            <select
              value={inviteProjectId}
              onChange={(event) => {
                setInviteProjectId(event.target.value);
                if (!event.target.value) {
                  setInviteReviewEnabled(false);
                }
              }}
            >
              <option value="">No project yet</option>
              {projects.map((project) => (
                <option key={project.project_id} value={project.project_id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          {inviteProjectId ? (
            <>
              <label>
                Project role
                <select
                  value={inviteProjectRole}
                  onChange={(event) => setInviteProjectRole(event.target.value)}
                >
                  <option value="contributor">contributor</option>
                  <option value="viewer">viewer</option>
                  <option value="owner">owner</option>
                </select>
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={inviteReviewEnabled}
                  onChange={(event) => setInviteReviewEnabled(event.target.checked)}
                />
                Daily review
              </label>
              {inviteReviewEnabled ? (
                <>
                  <label>
                    Review time
                    <input
                      type="time"
                      value={inviteReviewTime}
                      onChange={(event) => setInviteReviewTime(event.target.value)}
                    />
                  </label>
                  <label>
                    Timezone
                    <input
                      value={inviteReviewTimezone}
                      onChange={(event) => setInviteReviewTimezone(event.target.value)}
                    />
                  </label>
                </>
              ) : null}
            </>
          ) : null}
          <button className="btn-primary">Create invite</button>
        </form>
        {latestInvitation ? (
          <div className="invite-result">
            <div>
              <strong>{latestInvitation.email}</strong>
              <p className="subtle">Role: {latestInvitation.role}</p>
              {latestInvitation.project_role ? (
                <p className="subtle">
                  Project: {latestInvitation.project_role}
                  {latestInvitation.review_enabled
                    ? ` · daily at ${latestInvitation.review_run_at_local_time} (${latestInvitation.review_timezone_name})`
                    : ""}
                </p>
              ) : null}
            </div>
            {latestInvitation.warning ? (
              <p className="warn">{latestInvitation.warning}</p>
            ) : null}
            <label>
              Invite link
              <input value={latestInvitation.invite_url} readOnly />
            </label>
            <a className="btn-secondary" href={latestInvitation.mailto_url}>
              Email invite
            </a>
          </div>
        ) : null}
        {invitations.length ? (
          <div className="stack">
            {invitations.map((invitation) => (
              <article className="item" key={invitation.invitation_id}>
                <div className="item-head">
                  <div>
                    <strong>{invitation.email}</strong>
                    <p className="subtle">
                      {invitation.role} · expires {new Date(invitation.expires_at).toLocaleString()}
                    </p>
                  </div>
                  <span className={`pill status-${invitation.status}`}>
                    {invitation.status}
                  </span>
                </div>
                {invitation.status === "pending" ? (
                  <button
                    className="btn-secondary"
                    type="button"
                    onClick={() => revokeInvitation(invitation.invitation_id)}
                  >
                    Revoke invite
                  </button>
                ) : null}
              </article>
            ))}
          </div>
        ) : null}
      </section>
      <div className="stack">
        {users.map((user) => (
          <article className="item" key={user.user_id}>
            <div className="item-head">
              <div>
                <strong>{user.username}</strong>
                <p className="subtle">{user.user_id}</p>
              </div>
              <span className={`pill role-${user.role}`}>{user.role}</span>
            </div>
            <div className="user-admin-row">
              <label>
                Global role
                <select
                  value={user.role}
                  onChange={(event) =>
                    updateUser(user.user_id, { role: event.target.value }, "User role updated.")
                  }
                >
                  <option value="viewer">viewer</option>
                  <option value="editor">editor</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              <form className="inline" onSubmit={(event) => resetPassword(event, user.user_id)}>
                <label>
                  New password
                  <input
                    type="password"
                    value={passwordsByUser[user.user_id] || ""}
                    onChange={(event) => updatePasswordDraft(user.user_id, event.target.value)}
                    autoComplete="new-password"
                  />
                </label>
                <button className="btn-secondary">Reset password</button>
              </form>
            </div>
          </article>
        ))}
        {users.length === 0 ? <p className="subtle">No users found.</p> : null}
      </div>
    </article>
  );
}

export { UsersPage };
