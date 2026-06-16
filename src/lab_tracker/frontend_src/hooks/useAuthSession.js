import * as React from "react";

import { apiFetch, apiRequest } from "../shared/api.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

const { useEffect, useMemo, useState } = React;

function readInitialInvitation() {
  const params = new URLSearchParams(window.location.search || "");
  return {
    email: params.get("email") || "",
    token: params.get("invite") || "",
  };
}

function useAuthSession({ replace, setBusy, setFlash }) {
  const initialInvitation = readInitialInvitation();
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) || "");
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(true);

  const [authMode, setAuthMode] = useState(initialInvitation.token ? "register" : "login");
  const [authUsername, setAuthUsername] = useState(initialInvitation.email);
  const [authPassword, setAuthPassword] = useState("");
  const [authBootstrapToken, setAuthBootstrapToken] = useState("");
  const [authBootstrapStatus, setAuthBootstrapStatus] = useState(null);
  const [authInviteEmail] = useState(initialInvitation.email);
  const [authInviteToken, setAuthInviteToken] = useState(initialInvitation.token);
  const [authBusy, setAuthBusy] = useState(false);

  const canWrite = useMemo(
    () => Boolean(user && (user.role === "admin" || user.role === "editor")),
    [user]
  );

  useEffect(() => {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
      return;
    }
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }, [token]);

  useEffect(() => {
    let canceled = false;

    apiFetch("/auth/bootstrap-status")
      .then((payload) => {
        if (!canceled) {
          setAuthBootstrapStatus(payload?.data || null);
        }
      })
      .catch(() => {
        if (!canceled) {
          setAuthBootstrapStatus(null);
        }
      });

    setBusy(true);
    if (token) {
      setFlash("", "");
    }
    apiFetch("/auth/me", token ? { token } : {})
      .then((payload) => {
        if (!canceled) {
          const nextAuthEnabled = payload?.meta?.auth_enabled !== false;
          setAuthEnabled(nextAuthEnabled);
          setUser(payload?.data || null);
          if (!nextAuthEnabled && token) {
            setToken("");
          }
        }
      })
      .catch((err) => {
        if (!canceled) {
          setAuthEnabled(true);
          setUser(null);
          if (token && (err.status === 401 || err.status === 403)) {
            setToken("");
            setFlash("", err.message || "Failed to restore session.");
          } else if (token) {
            setFlash("", err.message || "Could not verify the saved session.");
          }
        }
      })
      .finally(() => {
        if (!canceled) {
          setAuthChecked(true);
          setBusy(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [setBusy, setFlash, token]);

  useEffect(() => {
    if (
      token ||
      authInviteToken ||
      authMode !== "login" ||
      !authBootstrapStatus?.first_admin_available
    ) {
      return;
    }
    setAuthMode("setup");
  }, [authBootstrapStatus, authInviteToken, authMode, token]);

  async function handleAuthSubmit(event) {
    event.preventDefault();
    if (!authUsername.trim() || !authPassword) {
      setFlash("", "Username and password are required.");
      return;
    }
    if (authMode === "setup" && !authBootstrapToken.trim()) {
      setFlash("", "Bootstrap token is required for the first admin account.");
      return;
    }

    setAuthBusy(true);
    setFlash("", "");
    try {
      const isRegistration = authMode === "register" || authMode === "setup";
      const payload = await apiRequest(isRegistration ? "/auth/register" : "/auth/login", {
        body: {
          ...(authMode === "setup"
            ? {
                bootstrap_token: authBootstrapToken.trim(),
                role: "admin",
              }
            : {}),
          ...(authMode === "register" && authInviteToken
            ? {
                invite_token: authInviteToken,
              }
            : {}),
          password: authPassword,
          username: authUsername.trim(),
        },
        method: "POST",
      });
      setToken(payload.access_token);
      setUser(payload.user || null);
      setAuthBootstrapToken("");
      setAuthInviteToken("");
      setAuthPassword("");
      if (authInviteToken) {
        replace("/app");
      }
      setFlash(
        authMode === "setup"
          ? "Admin account created. You are signed in."
          : authInviteToken
          ? "Invited account created. You are signed in."
          : authMode === "register"
          ? "Viewer account created. You are signed in."
          : "Signed in successfully."
      );
    } catch (err) {
      setFlash("", err.message || "Authentication failed.");
    } finally {
      setAuthBusy(false);
    }
  }

  function handleLogout() {
    if (!authEnabled) {
      replace("/app");
      return;
    }
    setToken("");
    setUser(null);
    setAuthBootstrapToken("");
    setAuthPassword("");
    replace("/app");
    setFlash("Signed out.", "");
  }

  return {
    authBusy,
    authBootstrapStatus,
    authBootstrapToken,
    authChecked,
    authEnabled,
    authInviteEmail,
    authInviteToken,
    authMode,
    authPassword,
    authUsername,
    canWrite,
    handleAuthSubmit,
    handleLogout,
    setAuthMode,
    setAuthBootstrapToken,
    setAuthPassword,
    setAuthUsername,
    token,
    user,
  };
}

export { useAuthSession };
