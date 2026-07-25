import * as React from "react";

import { apiListRequest, buildApiPath } from "../../shared/api.js";
import { AppLink } from "../../shared/routing.jsx";

const { useEffect, useState } = React;

function ExperimentChips({ token, entityType, entityId, navigate }) {
  const [state, setState] = useState({ error: "", items: [], loading: false });

  useEffect(() => {
    let canceled = false;
    if (!entityId || !["session", "dataset"].includes(entityType)) {
      setState({ error: "", items: [], loading: false });
      return () => {
        canceled = true;
      };
    }
    setState({ error: "", items: [], loading: true });
    apiListRequest(
      buildApiPath(`/${entityType}s/${entityId}/experiments`, {
        limit: 50,
        offset: 0,
      }),
      { token }
    )
      .then(({ data }) => {
        if (!canceled) {
          setState({ error: "", items: data, loading: false });
        }
      })
      .catch((err) => {
        if (!canceled) {
          setState({
            error: err.message || "Failed to load Experiment memberships.",
            items: [],
            loading: false,
          });
        }
      });
    return () => {
      canceled = true;
    };
  }, [entityId, entityType, token]);

  return (
    <div className="stack">
      <div className="item-head">
        <h3>Experiments</h3>
        <span className="pill">{state.items.length}</span>
      </div>
      {state.loading ? <p className="subtle">Loading Experiment memberships...</p> : null}
      {state.error ? <p className="flash error">{state.error}</p> : null}
      {!state.loading && !state.error && state.items.length === 0 ? (
        <p className="subtle">(not grouped into an Experiment)</p>
      ) : null}
      <div className="inline">
        {state.items.map((experiment) => (
          <AppLink
            key={experiment.experiment_id}
            to={`/app/experiments/${experiment.experiment_id}`}
            navigate={navigate}
            className="pill link"
          >
            {experiment.name}
          </AppLink>
        ))}
      </div>
    </div>
  );
}

export { ExperimentChips };
