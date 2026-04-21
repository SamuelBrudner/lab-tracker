import * as React from "react";

import { apiRequest } from "../shared/api.js";
import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

const { useEffect, useMemo, useState } = React;

function useAuthSession({ replace, setBusy, setFlash }) {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY) || "");
  const [user, setUser] = useState(null);

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

    if (!token) {
      setUser(null);
      return () => {
        canceled = true;
      };
    }

    setBusy(true);
    setFlash("", "");
    apiRequest("/auth/me", { token })
      .then((nextUser) => {
        if (!canceled) {
          setUser(nextUser);
        }
      })
      .catch((err) => {
        if (!canceled) {
          setToken("");
          setFlash("", err.message || "Failed to restore session.");
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
    setToken("");
    setUser(null);
    setAuthPassword("");
    replace("/app");
    setFlash("Signed out.", "");
  }

  return {
    authBusy,
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
