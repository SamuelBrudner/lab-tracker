import * as React from "react";

const { useCallback, useEffect, useState } = React;

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function parseAppRoute(pathname) {
  // `navigate()` passes the complete destination (including query/hash) while
  // popstate gives us `window.location.pathname`. Parse both forms identically
  // so contextual routes such as Capture can safely carry query state.
  const pathOnly = String(pathname || "").split(/[?#]/, 1)[0];
  const parts = pathOnly
    .split("/")
    .filter(Boolean);
  if (parts.length === 0) {
    return { kind: "home" };
  }
  const appIndex = parts.indexOf("app");
  if (appIndex < 0) {
    return { kind: "home" };
  }
  const routeParts = parts.slice(appIndex);
  if (routeParts.length === 1) {
    return { kind: "home" };
  }
  if (routeParts.length === 2 && routeParts[1] === "capture") {
    return { kind: "capture" };
  }
  if (routeParts.length === 2 && routeParts[1] === "devices") {
    return { kind: "devices" };
  }
  if (routeParts.length === 2 && routeParts[1] === "agents") {
    return { kind: "agents" };
  }
  if (routeParts.length === 2 && routeParts[1] === "setup") {
    return { kind: "setup" };
  }
  if (routeParts.length === 2 && routeParts[1] === "users") {
    return { kind: "users" };
  }
  if (routeParts.length === 2 && routeParts[1] === "enroll") {
    return { kind: "enroll" };
  }
  if (routeParts.length === 2 && routeParts[1] === "graph") {
    return { kind: "graph" };
  }
  if (routeParts.length === 2 && routeParts[1] === "batches") {
    return { kind: "batches" };
  }
  if (
    routeParts.length === 4 &&
    routeParts[1] === "projects" &&
    UUID_RE.test(routeParts[2] || "") &&
    routeParts[3] === "onboarding"
  ) {
    return { kind: "member-onboarding", projectId: routeParts[2] };
  }
  if (routeParts.length >= 3 && routeParts[1] === "batches" && UUID_RE.test(routeParts[2] || "")) {
    return { kind: "batch", changeSetId: routeParts[2] };
  }
  if (routeParts.length >= 3 && routeParts[1] === "questions" && UUID_RE.test(routeParts[2] || "")) {
    return { kind: "question", questionId: routeParts[2] };
  }
  if (routeParts.length >= 3 && routeParts[1] === "notes" && UUID_RE.test(routeParts[2] || "")) {
    return { kind: "note", noteId: routeParts[2] };
  }
  if (routeParts.length >= 3 && routeParts[1] === "sessions" && UUID_RE.test(routeParts[2] || "")) {
    return { kind: "session", sessionId: routeParts[2] };
  }
  if (routeParts.length >= 3 && routeParts[1] === "datasets" && UUID_RE.test(routeParts[2] || "")) {
    return { kind: "dataset", datasetId: routeParts[2] };
  }
  if (
    routeParts.length >= 3 &&
    routeParts[1] === "visualizations" &&
    UUID_RE.test(routeParts[2] || "")
  ) {
    return { kind: "visualization", vizId: routeParts[2] };
  }
  if (routeParts.length >= 3 && routeParts[1] === "goals" && UUID_RE.test(routeParts[2] || "")) {
    return { kind: "goal", goalId: routeParts[2] };
  }
  if (
    routeParts.length >= 3 &&
    routeParts[1] === "graph-drafts" &&
    UUID_RE.test(routeParts[2] || "")
  ) {
    return { kind: "graph-draft", changeSetId: routeParts[2] };
  }
  return { kind: "unknown", pathname: `/${routeParts.join("/")}` };
}

function appBasePath(pathname = window.location.pathname) {
  const parts = String(pathname || "")
    .split("/")
    .filter(Boolean);
  const appIndex = parts.indexOf("app");
  if (appIndex <= 0) {
    return "";
  }
  return `/${parts.slice(0, appIndex).join("/")}`;
}

function resolveAppPath(to, currentPathname = window.location.pathname) {
  const resolved = String(to || "/app");
  const basePath = appBasePath(currentPathname);
  if (!basePath || !resolved.startsWith("/app")) {
    return resolved;
  }
  if (resolved.startsWith(`${basePath}/app`)) {
    return resolved;
  }
  return `${basePath}${resolved}`;
}

function isContextualProjectReady({ projectId, projects, projectsLoaded, selectedProjectId }) {
  if (!projectId || selectedProjectId === projectId) {
    return true;
  }
  return Boolean(
    projectsLoaded && !projects.some((project) => project.project_id === projectId)
  );
}

function useAppRoute() {
  const [route, setRoute] = useState(() => parseAppRoute(window.location.pathname));

  useEffect(() => {
    function handlePopState() {
      setRoute(parseAppRoute(window.location.pathname));
    }

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = useCallback((to) => {
    const resolved = resolveAppPath(to);
    if (resolved === window.location.pathname) {
      return;
    }
    window.history.pushState({}, "", resolved);
    setRoute(parseAppRoute(resolved));
  }, []);

  const replace = useCallback((to) => {
    const resolved = resolveAppPath(to);
    window.history.replaceState({}, "", resolved);
    setRoute(parseAppRoute(resolved));
  }, []);

  return {
    navigate,
    replace,
    route,
  };
}

function AppLink({ to, navigate, className = "", children }) {
  // Resolve the deployed-prefix-aware URL ONCE and use the same value for both
  // the rendered href and the intercepted navigation, so copy-link, middle- and
  // modifier-clicks, and open-in-new-tab follow the identical prefixed path.
  const href = resolveAppPath(to);
  // Only internal app paths (which resolveAppPath returns as an absolute "/..."
  // path) are intercepted; an external/absolute URL is left to the browser.
  const isInternal = href.startsWith("/");
  return (
    <a
      href={href}
      className={className}
      onClick={(event) => {
        if (
          !isInternal ||
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.altKey ||
          event.ctrlKey ||
          event.shiftKey
        ) {
          return;
        }
        event.preventDefault();
        navigate(href);
      }}
    >
      {children}
    </a>
  );
}

export {
  AppLink,
  appBasePath,
  isContextualProjectReady,
  parseAppRoute,
  resolveAppPath,
  useAppRoute,
};
