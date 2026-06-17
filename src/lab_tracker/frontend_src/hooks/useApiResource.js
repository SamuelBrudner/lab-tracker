import * as React from "react";

import { apiRequest } from "../shared/api.js";

const { useCallback, useEffect, useState } = React;

function useApiResource(path, token, errorMessage) {
  const [state, setState] = useState({
    data: null,
    error: "",
    loading: false,
  });

  const setData = useCallback((nextData) => {
    setState((current) => ({
      ...current,
      data: typeof nextData === "function" ? nextData(current.data) : nextData,
      error: "",
    }));
  }, []);

  useEffect(() => {
    let canceled = false;
    if (!path) {
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

  return { ...state, setData };
}

export { useApiResource };
