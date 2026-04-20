function parseApiError(payload, fallbackMessage) {
  if (!payload || typeof payload !== "object") {
    return fallbackMessage;
  }
  if (payload.error && payload.error.message) {
    return payload.error.message;
  }
  return fallbackMessage;
}

async function apiRequest(path, options = {}) {
  const { method = "GET", token = "", body = null } = options;
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const headers = {
    Accept: "application/json",
  };

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== null && !isFormData) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, {
    method,
    headers,
    body: body === null ? undefined : isFormData ? body : JSON.stringify(body),
  });

  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const payload = isJson ? await response.json() : null;

  if (!response.ok) {
    const error = new Error(parseApiError(payload, `Request failed with ${response.status}`));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }

  if (!payload || !Object.prototype.hasOwnProperty.call(payload, "data")) {
    return null;
  }
  return payload.data;
}

function toBase64Content(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Unable to read file."));
    reader.onload = () => {
      const value = String(reader.result || "");
      const marker = value.indexOf(",");
      if (marker < 0) {
        reject(new Error("Unable to parse uploaded file."));
        return;
      }
      resolve(value.slice(marker + 1));
    };
    reader.readAsDataURL(file);
  });
}

export { apiRequest, parseApiError, toBase64Content };
