import * as React from "react";

import { memberOnboarding } from "../shared/gateways/index.js";

const { useCallback, useEffect, useRef, useState } = React;

function sameRequestContext(context, projectId, token) {
  return context.projectId === projectId && context.token === token;
}

function useMemberOnboarding({ projectId, token, setBusy, setFlash }) {
  const requestGeneration = useRef(0);
  const currentContextRef = useRef({ projectId, token });
  const [resource, setResource] = useState(() => ({
    data: null,
    error: "",
    loading: Boolean(projectId),
    projectId,
    token,
  }));

  const performLoad = useCallback(async ({ clearData = false } = {}) => {
    const requestedProjectId = projectId;
    const requestedToken = token;
    const generation = ++requestGeneration.current;
    if (!requestedProjectId) {
      setResource({
        data: null,
        error: "",
        loading: false,
        projectId: requestedProjectId,
        token: requestedToken,
      });
      return null;
    }
    setResource((current) => ({
      data:
        !clearData && sameRequestContext(current, requestedProjectId, requestedToken)
          ? current.data
          : null,
      error: "",
      loading: true,
      projectId: requestedProjectId,
      token: requestedToken,
    }));
    try {
      const next = await memberOnboarding.getMemberOnboarding(requestedProjectId, {
        token: requestedToken,
      });
      if (
        requestGeneration.current === generation &&
        sameRequestContext(currentContextRef.current, requestedProjectId, requestedToken)
      ) {
        setResource({
          data: next,
          error: "",
          loading: false,
          projectId: requestedProjectId,
          token: requestedToken,
        });
        return next;
      }
      return null;
    } catch (err) {
      if (
        requestGeneration.current === generation &&
        sameRequestContext(currentContextRef.current, requestedProjectId, requestedToken)
      ) {
        setResource((current) => ({
          ...current,
          error: err.message || "Unable to load project orientation.",
          loading: false,
        }));
      }
      return null;
    }
  }, [projectId, token]);

  const load = useCallback(() => performLoad(), [performLoad]);

  useEffect(() => {
    currentContextRef.current = { projectId, token };
    void performLoad({ clearData: true });
    return () => {
      requestGeneration.current += 1;
    };
  }, [performLoad, projectId, token]);

  const run = useCallback(
    async (command, successMessage) => {
      const requestedProjectId = projectId;
      const requestedToken = token;
      setBusy(true);
      setFlash("", "");
      try {
        const response = await command();
        if (!sameRequestContext(currentContextRef.current, requestedProjectId, requestedToken)) {
          return null;
        }
        const isOnboardingState = Boolean(
          response?.project_id === requestedProjectId && response?.capabilities
        );
        const next = isOnboardingState ? response : await performLoad();
        if (
          next?.project_id === requestedProjectId &&
          sameRequestContext(currentContextRef.current, requestedProjectId, requestedToken)
        ) {
          requestGeneration.current += 1;
          setResource({
            data: next,
            error: "",
            loading: false,
            projectId: requestedProjectId,
            token: requestedToken,
          });
        }
        if (!sameRequestContext(currentContextRef.current, requestedProjectId, requestedToken)) {
          return null;
        }
        setFlash(
          typeof successMessage === "function"
            ? successMessage(next, response)
            : successMessage
        );
        return next;
      } catch (err) {
        if (sameRequestContext(currentContextRef.current, requestedProjectId, requestedToken)) {
          setFlash("", err.message || "Project orientation could not be updated.");
        }
        throw err;
      } finally {
        setBusy(false);
      }
    },
    [performLoad, projectId, setBusy, setFlash, token]
  );

  const saveCheckpoint = useCallback(
    (fields) =>
      run(
        () => memberOnboarding.putCheckpoint(projectId, fields, { token }),
        "Tracking checkpoint saved."
      ),
    [projectId, run, token]
  );

  const startAiAlignment = useCallback(
    (externalProviderAcknowledged) =>
      run(
        () =>
          memberOnboarding.requestAiAlignment(
            projectId,
            externalProviderAcknowledged,
            { token }
          ),
        (next) => {
          const status = next?.alignment?.draft?.status;
          if (status === "drafting") {
            return "AI question alignment is being prepared. You can safely leave and resume later.";
          }
          if (status === "failed") {
            return "AI question alignment failed. Resolve the questions manually to continue.";
          }
          return "AI question alignment is ready for your review.";
        }
      ),
    [projectId, run, token]
  );

  const saveManualAlignment = useCallback(
    (resolutions) =>
      run(
        () => memberOnboarding.putManualAlignment(projectId, { resolutions }, { token }),
        "Question alignment saved."
      ),
    [projectId, run, token]
  );

  const resourceIsCurrent = sameRequestContext(resource, projectId, token);
  const onboarding = resourceIsCurrent ? resource.data : null;
  const loading = Boolean(projectId) && (!resourceIsCurrent || resource.loading);
  const error = resourceIsCurrent ? resource.error : "";

  return {
    error,
    load,
    loading,
    onboarding,
    saveCheckpoint,
    saveManualAlignment,
    startAiAlignment,
  };
}

export { useMemberOnboarding };
