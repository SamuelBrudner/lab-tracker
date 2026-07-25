import * as React from "react";
import * as ReactDOM from "react-dom/client";

import { App } from "./app-shell.jsx";
import { ErrorBoundary } from "./shared/ui.jsx";
import {
  applyServiceWorkerUpdate,
  registerServiceWorker,
} from "./shared/register-sw.js";
import { isStaticDemoEnabled } from "./shared/static-demo-api.js";

function AppRoot() {
  const [updateRegistration, setUpdateRegistration] = React.useState(null);

  React.useEffect(() => {
    if (!isStaticDemoEnabled()) {
      registerServiceWorker("/app/sw.js", {
        onUpdateReady: setUpdateRegistration,
      });
    }
  }, []);

  const reloadForUpdate = React.useCallback(() => {
    if (!applyServiceWorkerUpdate(updateRegistration)) {
      setUpdateRegistration(null);
    }
  }, [updateRegistration]);

  return <App onReloadForUpdate={updateRegistration ? reloadForUpdate : null} />;
}

const root = ReactDOM.createRoot(document.getElementById("app-root"));
root.render(
  <ErrorBoundary>
    <AppRoot />
  </ErrorBoundary>
);
