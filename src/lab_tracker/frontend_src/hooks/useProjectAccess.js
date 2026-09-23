import * as React from "react";

import { projects } from "../shared/gateways/index.js";

const { useCallback, useEffect, useRef, useState } = React;

function accessErrorMessage(err) {
  return `Could not confirm your access to this project: ${
    err?.message || "membership lookup failed."
  }`;
}

function roleContributes(userRole, projectRole) {
  return userRole === "admin" || projectRole === "contributor" || projectRole === "owner";
}

function roleManages(userRole, projectRole) {
  return userRole === "admin" || projectRole === "owner";
}

/**
 * One boundary for project-scoped access, keyed to a specific projectId.
 *
 * Membership requests are sequenced with a request-id ref so a late response for
 * a previously-selected project can never overwrite the current one, and the
 * previous project's role is cleared immediately on any change so controls never
 * flash-authorize from stale state. While access is unknown (loading), the
 * derived permissions deny; only a resolved answer for THIS projectId grants
 * access. Global admins are owner-equivalent without waiting on the fetch. The
 * server remains the authorization backstop — this only governs UI affordances.
 */
function useProjectAccess(projectId, { token, user, enabled = true } = {}) {
  const userId = user?.user_id || "";
  const userRole = user?.role || "";
  const [state, setState] = useState({
    projectId: null,
    status: "idle",
    role: "",
    members: [],
    error: "",
  });
  const requestIdRef = useRef(0);
  const [reloadNonce, setReloadNonce] = useState(0);
  const refresh = useCallback(() => setReloadNonce((nonce) => nonce + 1), []);

  useEffect(() => {
    const requestId = (requestIdRef.current += 1);
    if (!enabled || !projectId) {
      setState({
        projectId: projectId || null,
        status: "idle",
        role: "",
        members: [],
        error: "",
      });
      return undefined;
    }
    // Drop the previous project's membership immediately so nothing derived from
    // it survives into the new project's loading window.
    setState({ projectId, status: "loading", role: "", members: [], error: "" });
    let canceled = false;
    projects
      .listMembers(projectId, { token })
      .then(({ data }) => {
        if (canceled || requestId !== requestIdRef.current) {
          return; // a newer request (different project) superseded this one
        }
        const members = Array.isArray(data) ? data : [];
        const membership = members.find((member) => member.user_id === userId) || null;
        setState({
          projectId,
          status: "ready",
          role: membership?.role || "",
          members,
          error: "",
        });
      })
      .catch((err) => {
        if (canceled || requestId !== requestIdRef.current) {
          return;
        }
        // Stay fail-closed (no role) but say so: a transient lookup failure
        // must not silently look like a read-only membership.
        setState({
          projectId,
          status: "error",
          role: "",
          members: [],
          error: accessErrorMessage(err),
        });
      });
    return () => {
      canceled = true;
    };
  }, [projectId, token, userId, userRole, enabled, reloadNonce]);

  const isReady = state.status === "ready" && state.projectId === projectId;
  const isAdmin = userRole === "admin";
  return {
    projectId: state.projectId,
    status: state.status,
    role: state.role,
    members: state.members,
    error: state.projectId === projectId ? state.error : "",
    canContribute: isAdmin || (isReady && roleContributes(userRole, state.role)),
    canManage: isAdmin || (isReady && roleManages(userRole, state.role)),
    refresh,
  };
}

export { useProjectAccess };
