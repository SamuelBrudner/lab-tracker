import * as React from "react";

import { AUTH_REJECTED_EVENT } from "../shared/api.js";
import { auth as authGateway } from "../shared/gateways/index.js";
import { createAuthStorage, persistAuthSession } from "../shared/auth-storage.js";
import {
  TOKEN_EXPIRES_AT_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from "../shared/constants.js";
import { isStaticDemoEnabled } from "../shared/static-demo-api.js";

const { useCallback, useEffect, useMemo, useState } = React;
const REFRESH_MARGIN_MS = 5 * 60 * 1000;
const MIN_REFRESH_DELAY_MS = 60 * 1000;
const REFRESH_RETRY_MS = 60 * 1000;
const SESSION_EXPIRED_MESSAGE = "Your session expired. Please sign in again.";

function readInitialInvitation() {
  const params = new URLSearchParams(window.location.search || "");
  return {
    email: params.get("email") || "",
    token: params.get("invite") || "",
  };
}

function readInitialToken(storage) {
  return storage.getItem(TOKEN_STORAGE_KEY) || (isStaticDemoEnabled() ? "demo-token" : "");
}

function readInitialTokenExpiresAt(storage) {
  return storage.getItem(TOKEN_EXPIRES_AT_STORAGE_KEY) || "";
}

function parseExpiryMs(expiresAt) {
  const value = Date.parse(expiresAt || "");
  return Number.isFinite(value) ? value : null;
}

function refreshDelayMs(expiresAtMs, nowMs = Date.now()) {
  return Math.max(MIN_REFRESH_DELAY_MS, expiresAtMs - nowMs - REFRESH_MARGIN_MS);
}

function sessionExpiredMessage(message) {
  const text = String(message || "").trim();
  if (!text || /token|auth|credential|session/i.test(text)) {
    return SESSION_EXPIRED_MESSAGE;
  }
  return text;
}

function useAuthSession({ replace, setBusy, setFlash, storage }) {
  // One injected storage adapter for the tab; guards every read/write/remove and
  // falls back to memory so a persistence failure can never crash the session.
  const authStorage = useMemo(() => storage ?? createAuthStorage(), [storage]);
  const initialInvitation = readInitialInvitation();
  const [token, setToken] = useState(() => readInitialToken(authStorage));
  const [tokenExpiresAt, setTokenExpiresAt] = useState(() =>
    readInitialTokenExpiresAt(authStorage)
  );
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(true);
  const [persistenceDegraded, setPersistenceDegraded] = useState(false);

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

  const clearSession = useCallback(() => {
    setToken("");
    setTokenExpiresAt("");
    setUser(null);
  }, []);

  const applyAuthPayload = useCallback((payload) => {
    setToken(payload?.access_token || "");
    setTokenExpiresAt(payload?.expires_at || "");
    setUser(payload?.user || null);
  }, []);

  const persistTokenForReload = useCallback(
    (nextToken, nextExpiresAt = "") => {
      const persisted = persistAuthSession(authStorage, nextToken, nextExpiresAt);
      if (authStorage.isDegraded()) {
        setPersistenceDegraded(true);
      }
      return persisted;
    },
    [authStorage]
  );

  useEffect(() => {
    // Route persistence through the adapter: these writes/removes can no longer
    // throw into the error boundary. The in-memory state above stays the source
    // of truth, so a degraded write never signs the user out.
    if (token) {
      persistTokenForReload(token, tokenExpiresAt);
    } else {
      authStorage.removeItem(TOKEN_STORAGE_KEY);
      authStorage.removeItem(TOKEN_EXPIRES_AT_STORAGE_KEY);
    }
    if (authStorage.isDegraded()) {
      setPersistenceDegraded(true);
    }
  }, [authStorage, persistTokenForReload, token, tokenExpiresAt]);

  useEffect(() => {
    function handleAuthRejected(event) {
      const rejectedToken = event.detail?.token || "";
      if (!token || (rejectedToken && rejectedToken !== token)) {
        return;
      }
      clearSession();
      setFlash("", sessionExpiredMessage(event.detail?.message));
    }

    window.addEventListener(AUTH_REJECTED_EVENT, handleAuthRejected);
    return () => {
      window.removeEventListener(AUTH_REJECTED_EVENT, handleAuthRejected);
    };
  }, [clearSession, setFlash, token]);

  useEffect(() => {
    let canceled = false;

    authGateway
      .getBootstrapStatus()
      .then((status) => {
        if (!canceled) {
          setAuthBootstrapStatus(status);
          if (status?.bootstrap_token) {
            setAuthBootstrapToken((current) => current || status.bootstrap_token);
          }
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
    authGateway
      .getCurrentUser(token ? { notifyAuthRejected: false, token } : {})
      .then(({ authEnabled: nextAuthEnabled, user: nextUser }) => {
        if (!canceled) {
          setAuthEnabled(nextAuthEnabled);
          setUser(nextUser);
          if (!nextAuthEnabled && token) {
            setToken("");
            setTokenExpiresAt("");
          }
        }
      })
      .catch((err) => {
        if (!canceled) {
          setAuthEnabled(true);
          setUser(null);
          if (token && (err.status === 401 || err.status === 403)) {
            clearSession();
            setFlash("", sessionExpiredMessage(err.message));
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
  }, [clearSession, setBusy, setFlash, token]);

  useEffect(() => {
    if (!authEnabled || !token || !tokenExpiresAt || isStaticDemoEnabled()) {
      return undefined;
    }
    const expiresAtMs = parseExpiryMs(tokenExpiresAt);
    if (expiresAtMs === null) {
      return undefined;
    }

    let canceled = false;
    let timeoutId = null;

    function scheduleRefresh(delayMs) {
      timeoutId = window.setTimeout(refreshSession, delayMs);
    }

    async function refreshSession() {
      try {
        const payload = await authGateway.refreshSession({
          notifyAuthRejected: false,
          token,
        });
        if (!canceled) {
          applyAuthPayload(payload);
        }
      } catch (err) {
        if (canceled) {
          return;
        }
        if (err.status === 401 || err.status === 403) {
          clearSession();
          setFlash("", sessionExpiredMessage(err.message));
          return;
        }
        setFlash("", err.message || "Could not refresh the saved session. Lab Tracker will retry.");
        scheduleRefresh(REFRESH_RETRY_MS);
      }
    }

    scheduleRefresh(refreshDelayMs(expiresAtMs));
    return () => {
      canceled = true;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [
    applyAuthPayload,
    authEnabled,
    clearSession,
    setFlash,
    token,
    tokenExpiresAt,
  ]);

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
      // The session-issuing response is validated at the gateway: a malformed
      // token payload throws one ContractError instead of silently applying an
      // empty token / null user.
      const payload = await authGateway.authenticate(
        isRegistration ? "/auth/register" : "/auth/login",
        {
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
        }
      );
      applyAuthPayload(payload);
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
    clearSession();
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
    persistenceDegraded,
    persistTokenForReload,
    setAuthMode,
    setAuthBootstrapToken,
    setAuthPassword,
    setAuthUsername,
    token,
    tokenExpiresAt,
    user,
  };
}

export { MIN_REFRESH_DELAY_MS, useAuthSession };
