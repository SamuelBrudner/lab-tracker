import * as React from "react";

import { apiListRequest, apiRequest, buildApiPath } from "../shared/api.js";

function UsersPage({ token, canManageUsers, setBusy, setFlash }) {
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

  React.useEffect(() => {
    refreshUsers();
  }, [refreshUsers]);

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
