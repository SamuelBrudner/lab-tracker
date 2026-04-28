import * as React from "react";

import { apiFetch, apiRequest } from "../shared/api.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

const { useEffect, useMemo, useState } = React;

function useAuthSession({ replace, setBusy, setFlash }) {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) || "");
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(true);

  const [authMode, setAuthMode] = useState("login");
  const [authUsername, setAuthUsername] = useState("");
  const [authPassword, setAuthPassword] = useState("");
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
          if (token) {
            setToken("");
            setFlash("", err.message || "Failed to restore session.");
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

  async function handleAuthSubmit(event) {
    event.preventDefault();
    if (!authUsername.trim() || !authPassword) {
      setFlash("", "Username and password are required.");
      return;
    }

    setAuthBusy(true);
    setFlash("", "");
    try {
      const payload = await apiRequest(authMode === "register" ? "/auth/register" : "/auth/login", {
        body: {
          password: authPassword,
          username: authUsername.trim(),
        },
        method: "POST",
      });
      setToken(payload.access_token);
      setUser(payload.user || null);
      setAuthPassword("");
      setFlash(
        authMode === "register"
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
    setAuthPassword("");
    replace("/app");
    setFlash("Signed out.", "");
  }

  return {
    authBusy,
    authChecked,
    authEnabled,
    authMode,
    authPassword,
    authUsername,
    canWrite,
    handleAuthSubmit,
    handleLogout,
    setAuthMode,
    setAuthPassword,
    setAuthUsername,
    token,
    user,
  };
}

export { useAuthSession };
