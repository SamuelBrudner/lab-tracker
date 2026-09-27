import * as React from "react";

import { INVITED_PASSWORD_MIN_LENGTH } from "./constants.js";
import { formatDate, roleClass } from "./formatters.js";

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = {
      hasError: false,
      errorMessage: "",
      componentStack: "",
      resetKey: 0,
    };
    this.handleRetry = this.handleRetry.bind(this);
    this.handleReload = this.handleReload.bind(this);
  }

  static getDerivedStateFromError(error) {
    let errorMessage = "Unknown error.";
    if (error instanceof Error && error.message) {
      errorMessage = error.message;
    } else if (typeof error === "string") {
      errorMessage = error;
    } else if (error && typeof error === "object" && "message" in error) {
      errorMessage = String(error.message || errorMessage);
    } else if (error) {
      errorMessage = String(error);
    }

    return { hasError: true, errorMessage };
  }

  componentDidCatch(error, errorInfo) {
    // eslint-disable-next-line no-console
    console.error("React rendering error:", error, errorInfo);
    const componentStack =
      errorInfo && typeof errorInfo.componentStack === "string" ? errorInfo.componentStack : "";
    if (componentStack) {
      this.setState({ componentStack });
    }
  }

  handleRetry() {
    this.setState((current) => ({
      hasError: false,
      errorMessage: "",
      componentStack: "",
      resetKey: current.resetKey + 1,
    }));
  }

  handleReload() {
    window.location.reload();
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="app-shell">
          <header className="hero">
            <div className="hero-row">
              <div>
                <h1>Lab Tracker</h1>
                <p className="subtle">The app hit an unexpected error.</p>
              </div>
            </div>
          </header>

          <section className="grid">
            <article className="card span-12">
              <h2>Something went wrong</h2>
              <p className="subtle">
                Click &quot;Try again&quot; to re-render the app. If the problem persists, reload the
                page.
              </p>

              <div className="inline">
                <button type="button" className="btn-primary" onClick={this.handleRetry}>
                  Try again
                </button>
                <button type="button" className="btn-secondary" onClick={this.handleReload}>
                  Reload page
                </button>
              </div>

              {this.state.errorMessage ? (
                <p className="flash error">Error: {this.state.errorMessage}</p>
              ) : null}

              {process.env.NODE_ENV !== "production" && this.state.componentStack ? (
                <details className="subtle">
                  <summary>Details</summary>
                  <pre className="mono">{this.state.componentStack}</pre>
                </details>
              ) : null}
            </article>
          </section>
        </div>
      );
    }

    return <React.Fragment key={this.state.resetKey}>{this.props.children}</React.Fragment>;
  }
}

function AppHeader({ activeKind, authEnabled, navigate, user, onLogout }) {
  return (
    <header className="hero">
      <div className="hero-row">
        <div>
          <h1>Lab Tracker</h1>
          <p className="subtle">
            Projects, questions, notes, sessions, datasets, and analysis records.
          </p>
        </div>
        <div className="inline">
          {user ? <span className={roleClass(user.role)}>{user.role}</span> : null}
          {user ? <span className="pill">{user.username}</span> : null}
          {authEnabled && user ? (
            <button className="btn-secondary" onClick={onLogout}>
              Sign out
            </button>
          ) : null}
        </div>
      </div>
      {user ? (
        <AppNavigation
          activeKind={activeKind}
          isAdmin={user.role === "admin"}
          navigate={navigate}
        />
      ) : null}
    </header>
  );
}

// The brain of the app in the nav: capture, review, recall. Configuration
// pages step back into a Settings group.
const PRIMARY_LINKS = [
  ["home", "/app", "Home"],
  ["capture", "/app/capture", "Capture"],
  ["batches", "/app/batches", "Review"],
  ["graph", "/app/graph", "Graph"],
];
const SETTINGS_LINKS = [
  ["devices", "/app/devices", "Devices"],
  ["agents", "/app/agents", "Agents"],
  ["setup", "/app/setup", "Setup"],
];
const ADMIN_SETTINGS_LINKS = [["users", "/app/users", "Users"]];
// Route kinds that belong to the Review entry: the queue, one batch, one draft.
const REVIEW_KINDS = new Set(["batches", "batch", "graph-draft"]);
const SETTINGS_KINDS = new Set(["devices", "agents", "setup", "users"]);

function NavLink({ active, label, onClick }) {
  return (
    <button
      type="button"
      className={`app-nav-link${active ? " active" : ""}`}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function AppNavigation({ activeKind, isAdmin = false, navigate }) {
  const activeKey = REVIEW_KINDS.has(activeKind) ? "batches" : activeKind;
  const settingsActive = SETTINGS_KINDS.has(activeKind);
  const settingsLinks = isAdmin ? [...SETTINGS_LINKS, ...ADMIN_SETTINGS_LINKS] : SETTINGS_LINKS;
  return (
    <nav className="app-nav" aria-label="Primary">
      {PRIMARY_LINKS.map(([kind, path, label]) => (
        <NavLink
          key={kind}
          active={activeKey === kind}
          label={label}
          onClick={() => navigate(path)}
        />
      ))}
      <details className="app-nav-group" open={settingsActive}>
        <summary className={`app-nav-link${settingsActive ? " active" : ""}`}>Settings</summary>
        <div className="app-nav-group-links">
          {settingsLinks.map(([kind, path, label]) => (
            <NavLink
              key={kind}
              active={activeKey === kind}
              label={label}
              onClick={() => navigate(path)}
            />
          ))}
        </div>
      </details>
    </nav>
  );
}

function FlashMessages({ message, error }) {
  if (!message && !error) {
    return null;
  }

  // Announced to assistive tech: a decision made far down the page is
  // confirmed here without the person having to scroll up to check.
  return (
    <>
      {message ? (
        <p className="flash ok" role="status" aria-live="polite">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="flash error" role="alert">
          {error}
        </p>
      ) : null}
    </>
  );
}

/**
 * Offers text recovered from local storage after a tab closed mid-edit.
 *
 * Deliberately an offer rather than an automatic restore: the recovered text may
 * be older than what the server now holds, so the person decides. `onRestore`
 * only fills the editor — nothing is submitted on their behalf.
 */
function DraftRecoveryNotice({ label = "unsaved changes", savedAt, onRestore, onDiscard }) {
  if (!savedAt) {
    return null;
  }

  return (
    <div className="flash draft-recovery" role="status">
      <span>
        You have {label} from {formatDate(new Date(savedAt).toISOString())} that were never
        saved.
      </span>
      <button type="button" className="btn-secondary" onClick={onRestore}>
        Restore them
      </button>
      <button type="button" className="btn-secondary" onClick={onDiscard}>
        Discard them
      </button>
    </div>
  );
}

function UpdateAvailableBanner({ onReload }) {
  if (typeof onReload !== "function") {
    return null;
  }

  return (
    <div className="flash ok app-update-banner" role="status">
      <span>An updated version of Lab Tracker is ready.</span>
      <button type="button" className="btn-primary" onClick={onReload}>
        Reload to update
      </button>
    </div>
  );
}

function AuthForm({
  authBootstrapStatus,
  authBootstrapToken,
  authInviteEmail,
  authInviteToken,
  authMode,
  authUsername,
  authPassword,
  authPasswordConfirmation,
  authBusy,
  onBootstrapTokenChange,
  onSubmit,
  onUsernameChange,
  onPasswordChange,
  onPasswordConfirmationChange,
  onToggleMode,
}) {
  const isSetup = authMode === "setup";
  const isInvite = authMode === "register" && Boolean(authInviteToken);
  const bootstrapTokenLoaded = Boolean(authBootstrapStatus?.bootstrap_token);
  const title = isSetup
    ? "Create First Admin"
    : isInvite
    ? "Accept Invitation"
    : authMode === "login"
    ? "Sign In"
    : "Create Viewer Account";
  const supportingCopy = isSetup
    ? bootstrapTokenLoaded
      ? "The first-admin token is loaded for this setup screen."
      : "Enter the first-admin token for this deployment."
    : isInvite
    ? `Choose at least ${INVITED_PASSWORD_MIN_LENGTH} characters for the invited account, then confirm the password.`
    : "Viewer registration is public. Admin/editor accounts must be provisioned by an admin.";
  return (
    <article className="card span-6">
      <h2>{title}</h2>
      <p className="subtle">{supportingCopy}</p>
      <form className="form" onSubmit={onSubmit}>
        <label>
          Username
          <input
            value={authUsername}
            onChange={onUsernameChange}
            autoComplete="username"
            readOnly={isInvite && Boolean(authInviteEmail)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={authPassword}
            onChange={onPasswordChange}
            autoComplete={authMode === "login" ? "current-password" : "new-password"}
            minLength={isInvite ? INVITED_PASSWORD_MIN_LENGTH : undefined}
          />
        </label>
        {isInvite ? (
          <label>
            Confirm password
            <input
              type="password"
              value={authPasswordConfirmation}
              onChange={onPasswordConfirmationChange}
              autoComplete="new-password"
              minLength={INVITED_PASSWORD_MIN_LENGTH}
            />
          </label>
        ) : null}
        {isSetup ? (
          <label>
            Bootstrap token
            <input
              type="password"
              value={authBootstrapToken}
              onChange={onBootstrapTokenChange}
              autoComplete="one-time-code"
              placeholder={bootstrapTokenLoaded ? "Loaded for first admin setup" : ""}
            />
          </label>
        ) : null}
        {isSetup && authBootstrapStatus?.bootstrap_token_warning ? (
          <p className="warn">{authBootstrapStatus.bootstrap_token_warning}</p>
        ) : null}
        {!isSetup && authBootstrapStatus?.first_admin_available ? (
          <p className="warn">This instance has no admin yet. Use first-admin setup first.</p>
        ) : null}
        <div className="inline">
          <button className="btn-primary" disabled={authBusy}>
            {authBusy
              ? "Working..."
              : isSetup
              ? "Create admin"
              : isInvite
              ? "Create account"
              : authMode === "login"
              ? "Sign in"
              : "Register"}
          </button>
          {!isInvite ? (
            <button type="button" className="btn-secondary" onClick={onToggleMode}>
              {authMode === "login" ? "Need an account?" : "Have an account?"}
            </button>
          ) : null}
        </div>
      </form>
    </article>
  );
}

function RequestEditAccess({ selectedProject }) {
  const subject = encodeURIComponent("Lab Tracker edit access request");
  const body = encodeURIComponent(
    [
      "Please grant editor access for Lab Tracker.",
      selectedProject ? `Project: ${selectedProject.name}` : "",
      `Page: ${window.location.href}`,
    ]
      .filter(Boolean)
      .join("\n")
  );
  return (
    <a className="btn-secondary request-access" href={`mailto:?subject=${subject}&body=${body}`}>
      Request edit access
    </a>
  );
}

function WorkflowCoverageCard() {
  return (
    <article className="card span-6">
      <h2>Capture, review, recall</h2>
      <div className="stack">
        <div className="item">
          1. Capture — commits, figures, watched folders, bench notes and phone captures queue
          offline and land in the inbox as staged evidence.
        </div>
        <div className="item">
          2. Review — a person accepts, edits or sets aside each proposal; Lab Tracker suggests,
          only you commit.
        </div>
        <div className="item">
          3. Recall — questions, claims and the project graph give the reasoning back when you
          need it.
        </div>
      </div>
    </article>
  );
}

function ProjectContextCard({ selectedProject }) {
  return (
    <article className="card span-12">
      <h2>Project Context</h2>
      {selectedProject ? (
        <div>
          <strong>{selectedProject.name}</strong>
          <p>{selectedProject.description || "No project description."}</p>
          <p className="mono">{selectedProject.project_id}</p>
        </div>
      ) : (
        <p className="subtle">Create or select a project to start the workflow.</p>
      )}
    </article>
  );
}

function UnknownRouteCard({ pathname, navigate }) {
  return (
    <article className="card span-8">
      <h2>Unknown View</h2>
      <p className="subtle">No route matches: {pathname}</p>
      <button type="button" className="btn-secondary" onClick={() => navigate("/app")}>
        Back to dashboard
      </button>
    </article>
  );
}

export {
  AppNavigation,
  AppHeader,
  AuthForm,
  DraftRecoveryNotice,
  ErrorBoundary,
  FlashMessages,
  ProjectContextCard,
  RequestEditAccess,
  UpdateAvailableBanner,
  UnknownRouteCard,
  WorkflowCoverageCard,
};
