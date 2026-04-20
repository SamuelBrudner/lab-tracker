import * as React from "react";

const { useCallback, useEffect, useState } = React;

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function parseAppRoute(pathname) {
  const parts = String(pathname || "")
    .split("/")
    .filter(Boolean);
  if (parts.length === 0) {
    return { kind: "home" };
  }
  if (parts[0] !== "app") {
    return { kind: "home" };
  }
  if (parts.length === 1) {
    return { kind: "home" };
  }
  if (parts.length >= 3 && parts[1] === "questions" && UUID_RE.test(parts[2] || "")) {
    return { kind: "question", questionId: parts[2] };
  }
  if (parts.length >= 3 && parts[1] === "notes" && UUID_RE.test(parts[2] || "")) {
    return { kind: "note", noteId: parts[2] };
  }
  if (parts.length >= 3 && parts[1] === "sessions" && UUID_RE.test(parts[2] || "")) {
    return { kind: "session", sessionId: parts[2] };
  }
  if (parts.length >= 3 && parts[1] === "datasets" && UUID_RE.test(parts[2] || "")) {
    return { kind: "dataset", datasetId: parts[2] };
  }
  if (parts.length >= 3 && parts[1] === "visualizations" && UUID_RE.test(parts[2] || "")) {
    return { kind: "visualization", vizId: parts[2] };
  }
  return { kind: "unknown", pathname: `/${parts.join("/")}` };
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
    const resolved = String(to || "/app");
    if (resolved === window.location.pathname) {
      return;
    }
    window.history.pushState({}, "", resolved);
    setRoute(parseAppRoute(resolved));
  }, []);

  const replace = useCallback((to) => {
    const resolved = String(to || "/app");
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
  return (
    <a
      href={to}
      className={className}
      onClick={(event) => {
        if (
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
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

export { AppLink, parseAppRoute, useAppRoute };
