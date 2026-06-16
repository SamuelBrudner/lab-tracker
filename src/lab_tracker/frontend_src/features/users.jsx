import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";

function UsersPage({ token, canManageUsers, setBusy, setFlash }) {
  const [inviteEmail, setInviteEmail] = React.useState("");
  const [inviteRole, setInviteRole] = React.useState("editor");
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
          <button className="btn-primary">Create invite</button>
        </form>
        {latestInvitation ? (
          <div className="invite-result">
            <div>
              <strong>{latestInvitation.email}</strong>
              <p className="subtle">Role: {latestInvitation.role}</p>
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
