// Typed gateway for the auth domain.
import { apiFetch } from "../api.js";
import { object, optional, parseResource, string } from "../contract.js";

// The signed-in user embedded in a token response. user_id is the identity the
// rest of the app keys access decisions on, so it is required.
const authUserShape = object({
  user_id: string,
  username: optional(string),
  role: optional(string),
});

// The login / register token response. A malformed payload here would otherwise
// leave the session with an empty token or null user and silently sign-in-fail;
// validation turns that into one explicit contract error.
const authTokenShape = object({
  access_token: string,
  user: authUserShape,
  token_type: optional(string),
  expires_at: optional(string),
});

// POST credentials to a session-issuing endpoint (/auth/login or /auth/register)
// and return the validated token payload.
async function authenticate(path, body, options = {}) {
  const envelope = await apiFetch(path, { ...options, body, method: "POST" });
  return parseResource(envelope, authTokenShape);
}

export { authTokenShape, authUserShape, authenticate };
