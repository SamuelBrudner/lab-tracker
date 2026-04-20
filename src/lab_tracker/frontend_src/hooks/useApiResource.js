import * as React from "react";

import { apiRequest } from "../shared/api.js";

const { useEffect, useState } = React;

function useApiResource(path, token, errorMessage) {
  const [state, setState] = useState({
    data: null,
    error: "",
    loading: false,
  });

  useEffect(() => {
    let canceled = false;
    if (!token || !path) {
      setState({ data: null, error: "", loading: false });
      return () => {
        canceled = true;
      };
    }

    setState({ data: null, error: "", loading: true });
    apiRequest(path, { token })
      .then((payload) => {
        if (!canceled) {
          setState({ data: payload, error: "", loading: false });
        }
      })
      .catch((err) => {
        if (!canceled) {
          setState({
            data: null,
            error: err.message || errorMessage,
            loading: false,
          });
        }
      });

    return () => {
      canceled = true;
    };
  }, [errorMessage, path, token]);

  return state;
}

export { useApiResource };
