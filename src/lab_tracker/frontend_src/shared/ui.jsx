import * as React from "react";

import { roleClass } from "./formatters.js";

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
                <h1>Lab Tracker Frontend MVP</h1>
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

function AppHeader({ authEnabled, user, onLogout }) {
  return (
    <header className="hero">
      <div className="hero-row">
        <div>
          <h1>Lab Tracker Frontend MVP</h1>
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
    </header>
  );
}

function FlashMessages({ message, error }) {
  if (!message && !error) {
    return null;
  }

  return (
    <>
      {message ? <p className="flash ok">{message}</p> : null}
      {error ? <p className="flash error">{error}</p> : null}
    </>
  );
}

function AuthForm({
  authMode,
  authUsername,
  authPassword,
  authBusy,
  onSubmit,
  onUsernameChange,
  onPasswordChange,
  onToggleMode,
}) {
  return (
    <article className="card span-6">
      <h2>{authMode === "login" ? "Sign In" : "Create Viewer Account"}</h2>
      <p className="subtle">
        Viewer registration is public. Admin/editor accounts must be provisioned by an admin.
      </p>
      <form className="form" onSubmit={onSubmit}>
        <label>
          Username
          <input value={authUsername} onChange={onUsernameChange} autoComplete="username" />
        </label>
        <label>
          Password
          <input
            type="password"
            value={authPassword}
            onChange={onPasswordChange}
            autoComplete={authMode === "login" ? "current-password" : "new-password"}
          />
        </label>
        <div className="inline">
          <button className="btn-primary" disabled={authBusy}>
            {authBusy ? "Working..." : authMode === "login" ? "Sign in" : "Register"}
          </button>
          <button type="button" className="btn-secondary" onClick={onToggleMode}>
            {authMode === "login" ? "Need an account?" : "Have an account?"}
          </button>
        </div>
      </form>
    </article>
  );
}

function WorkflowCoverageCard() {
  return (
    <article className="card span-6">
      <h2>Workflow Coverage</h2>
      <div className="stack">
        <div className="item">1. Project dashboard and project creation</div>
        <div className="item">2. Manual question capture and explicit activation</div>
        <div className="item">3. Note capture, raw file upload, and download-ready records</div>
        <div className="item">4. Sessions, dataset commit, and explicit analysis registration</div>
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
  AppHeader,
  AuthForm,
  ErrorBoundary,
  FlashMessages,
  ProjectContextCard,
  UnknownRouteCard,
  WorkflowCoverageCard,
};
