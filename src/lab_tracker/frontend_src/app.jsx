import * as React from "react";
import * as ReactDOM from "react-dom/client";

import { App } from "./app-shell.jsx";
import { ErrorBoundary } from "./shared/ui.jsx";
import { registerServiceWorker } from "./shared/register-sw.js";
import { isStaticDemoEnabled } from "./shared/static-demo-api.js";

if (!isStaticDemoEnabled()) {
  registerServiceWorker();
}

const root = ReactDOM.createRoot(document.getElementById("app-root"));
root.render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);
