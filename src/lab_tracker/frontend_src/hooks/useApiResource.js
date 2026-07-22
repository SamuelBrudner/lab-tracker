import * as React from "react";

import { apiRequest } from "../shared/api.js";

const { useCallback, useEffect, useState } = React;

function useApiResource(path, token, errorMessage, { validate = null } = {}) {
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
        // When a validator is supplied, enforce the shape at the boundary: a
        // malformed 2xx envelope (already collapsed to null by apiRequest, or
        // present with the wrong shape) throws one ContractError instead of
        // flowing null/mis-shaped data into the component.
        const data = validate ? validate(payload) : payload;
        if (!canceled) {
          setState({ data, error: "", loading: false });
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
  }, [errorMessage, path, token, validate]);

  return { ...state, setData };
}

export { useApiResource };
