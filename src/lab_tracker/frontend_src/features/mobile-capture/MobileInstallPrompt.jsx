import * as React from "react";

const { useEffect, useState } = React;

const INSTALL_PROMPT_DISMISSED_KEY = "lab-tracker-install-prompt-dismissed";

function readInstallPromptDismissed() {
  try {
    return localStorage.getItem(INSTALL_PROMPT_DISMISSED_KEY) === "true";
  } catch {
    return false;
  }
}

function rememberInstallPromptDismissed() {
  try {
    localStorage.setItem(INSTALL_PROMPT_DISMISSED_KEY, "true");
  } catch {
    // Storage may be unavailable in private browsing; session state still hides it.
  }
}

function isStandaloneApp() {
  if (typeof window === "undefined") {
    return false;
  }
  return Boolean(
    window.matchMedia?.("(display-mode: standalone)")?.matches || window.navigator?.standalone
  );
}

function isPhoneSizedBrowser() {
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    return false;
  }
  const ua = String(navigator.userAgent || "");
  return (
    /Android|iPhone|iPad|iPod/i.test(ua) ||
    window.matchMedia?.("(pointer: coarse)")?.matches ||
    window.innerWidth <= 760
  );
}

function readInstallIntent() {
  try {
    return new URLSearchParams(window.location.search || "").get("install") === "1";
  } catch {
    return false;
  }
}

function MobileInstallPrompt() {
  const [dismissed, setDismissed] = useState(() => readInstallPromptDismissed());
  const [visible, setVisible] = useState(false);
  const [nativePrompt, setNativePrompt] = useState(null);
  const [showSteps, setShowSteps] = useState(false);

  useEffect(() => {
    function refreshVisibility() {
      setVisible(
        !readInstallPromptDismissed() &&
          !isStandaloneApp() &&
          (readInstallIntent() || isPhoneSizedBrowser())
      );
    }

    function handleBeforeInstallPrompt(event) {
      event.preventDefault();
      setNativePrompt(event);
      refreshVisibility();
    }

    function handleInstalled() {
      rememberInstallPromptDismissed();
      setDismissed(true);
      setVisible(false);
    }

    refreshVisibility();
    window.addEventListener("resize", refreshVisibility);
    window.addEventListener("orientationchange", refreshVisibility);
    window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
    window.addEventListener("appinstalled", handleInstalled);
    return () => {
      window.removeEventListener("resize", refreshVisibility);
      window.removeEventListener("orientationchange", refreshVisibility);
      window.removeEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
      window.removeEventListener("appinstalled", handleInstalled);
    };
  }, []);

  if (dismissed || !visible) {
    return null;
  }

  function dismiss({ remember = false } = {}) {
    if (remember) {
      rememberInstallPromptDismissed();
    }
    setDismissed(true);
    setVisible(false);
  }

  async function installOrShowSteps() {
    if (nativePrompt?.prompt) {
      nativePrompt.prompt();
      try {
        const choice = await nativePrompt.userChoice;
        if (choice?.outcome === "accepted") {
          dismiss({ remember: true });
          return;
        }
      } catch {
        // Fall back to manual steps below.
      } finally {
        setNativePrompt(null);
      }
    }
    setShowSteps(true);
  }

  return (
    <aside className="install-nudge" role="status">
      <div>
        <h3>Add Lab Tracker to this phone</h3>
        <p className="subtle">Open capture from the Home Screen instead of rescanning the QR.</p>
      </div>
      {showSteps ? (
        <ol className="install-steps">
          <li>Tap the Safari share button.</li>
          <li>Choose Add to Home Screen.</li>
          <li>Tap Add.</li>
        </ol>
      ) : null}
      <div className="install-actions">
        <button className="btn-primary" onClick={installOrShowSteps} type="button">
          Add icon
        </button>
        <button className="btn-secondary" onClick={() => dismiss()} type="button">
          Not now
        </button>
        <button className="btn-link" onClick={() => dismiss({ remember: true })} type="button">
          Don't show again
        </button>
      </div>
    </aside>
  );
}

export { MobileInstallPrompt };
