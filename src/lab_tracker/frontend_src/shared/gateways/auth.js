// @ts-check

// Typed gateway for every auth-domain response consumed by the frontend. The
// typedefs are generated from FastAPI's OpenAPI document; runtime validators
// remain explicit so a malformed 2xx response fails at the network boundary.
import { apiFetch, buildApiPath } from "../api.js";
import {
  boolean,
  nullish,
  object,
  oneOf,
  optional,
  parseCollection,
  parseResource,
  parseResourceWithMeta,
  string,
} from "../contract.js";

/** @typedef {import("../../generated/openapi.js").operations["auth_bootstrap_status_auth_bootstrap_status_get"]["responses"][200]["content"]["application/json"]["data"]} AuthBootstrapStatus */
/** @typedef {import("../../generated/openapi.js").operations["create_auth_invitation_auth_invitations_post"]["responses"][201]["content"]["application/json"]["data"]} CreatedAuthInvitation */
/** @typedef {import("../../generated/openapi.js").operations["list_auth_invitations_auth_invitations_get"]["responses"][200]["content"]["application/json"]["data"][number]} ListedAuthInvitation */
/** @typedef {import("../../generated/openapi.js").operations["revoke_auth_invitation_auth_invitations__invitation_id__delete"]["responses"][200]["content"]["application/json"]["data"]} RevokedAuthInvitation */
/** @typedef {CreatedAuthInvitation & ListedAuthInvitation & RevokedAuthInvitation} AuthInvitationRead */
/** @typedef {import("../../generated/openapi.js").operations["login_auth_auth_login_post"]["responses"][200]["content"]["application/json"]["data"]} LoginAuthToken */
/** @typedef {import("../../generated/openapi.js").operations["register_auth_auth_register_post"]["responses"][201]["content"]["application/json"]["data"]} RegistrationAuthToken */
/** @typedef {import("../../generated/openapi.js").operations["refresh_auth_auth_refresh_post"]["responses"][200]["content"]["application/json"]["data"]} RefreshedAuthToken */
/** @typedef {LoginAuthToken & RegistrationAuthToken & RefreshedAuthToken} AuthTokenRead */
/** @typedef {import("../../generated/openapi.js").operations["auth_me_auth_me_get"]["responses"][200]["content"]["application/json"]["data"]} CurrentAuthUser */
/** @typedef {import("../../generated/openapi.js").operations["list_auth_users_auth_users_get"]["responses"][200]["content"]["application/json"]["data"][number]} ListedAuthUser */
/** @typedef {import("../../generated/openapi.js").operations["update_auth_user_auth_users__user_id__patch"]["responses"][200]["content"]["application/json"]["data"]} UpdatedAuthUser */
/** @typedef {CurrentAuthUser & ListedAuthUser & UpdatedAuthUser} AuthUserRead */
/** @typedef {import("../../generated/openapi.js").operations["auth_setup_readiness_auth_setup_readiness_get"]["responses"][200]["content"]["application/json"]["data"]} AuthSetupReadiness */
/** @typedef {import("../../generated/openapi.js").operations["consume_enrollment_auth_devices_consume_post"]["responses"][201]["content"]["application/json"]["data"]} DeviceConsumeRead */
/** @typedef {import("../../generated/openapi.js").operations["create_enrollment_auth_devices_enrollment_post"]["responses"][201]["content"]["application/json"]["data"]} DeviceEnrollmentRead */
/** @typedef {import("../../generated/openapi.js").operations["list_devices_auth_devices_get"]["responses"][200]["content"]["application/json"]["data"][number]} ListedDeviceToken */
/** @typedef {import("../../generated/openapi.js").operations["revoke_device_auth_devices__device_token_id__delete"]["responses"][200]["content"]["application/json"]["data"]} RevokedDeviceToken */
/** @typedef {ListedDeviceToken & RevokedDeviceToken} DeviceTokenRead */
/** @typedef {import("../../generated/openapi.js").operations["create_personal_access_token_auth_tokens_post"]["responses"][201]["content"]["application/json"]["data"]} PersonalAccessTokenIssuedRead */
/** @typedef {import("../../generated/openapi.js").operations["list_personal_access_tokens_auth_tokens_get"]["responses"][200]["content"]["application/json"]["data"][number]} ListedPersonalAccessToken */
/** @typedef {import("../../generated/openapi.js").operations["revoke_personal_access_token_auth_tokens__token_id__delete"]["responses"][200]["content"]["application/json"]["data"]} RevokedPersonalAccessToken */
/** @typedef {ListedPersonalAccessToken & RevokedPersonalAccessToken} PersonalAccessTokenRead */
/** @typedef {import("../contract.js").Validator<AuthBootstrapStatus>} AuthBootstrapStatusValidator */
/** @typedef {import("../contract.js").Validator<AuthInvitationRead>} AuthInvitationValidator */
/** @typedef {import("../contract.js").Validator<AuthTokenRead>} AuthTokenValidator */
/** @typedef {import("../contract.js").Validator<AuthUserRead>} AuthUserValidator */
/** @typedef {import("../contract.js").Validator<AuthSetupReadiness>} AuthSetupReadinessValidator */
/** @typedef {import("../contract.js").Validator<DeviceConsumeRead>} DeviceConsumeValidator */
/** @typedef {import("../contract.js").Validator<DeviceEnrollmentRead>} DeviceEnrollmentValidator */
/** @typedef {import("../contract.js").Validator<DeviceTokenRead>} DeviceTokenValidator */
/** @typedef {import("../contract.js").Validator<PersonalAccessTokenIssuedRead>} PersonalAccessTokenIssuedValidator */
/** @typedef {import("../contract.js").Validator<PersonalAccessTokenRead>} PersonalAccessTokenValidator */

const roleShape = /** @type {import("../contract.js").Validator<AuthUserRead["role"]>} */ (
  oneOf("admin", "editor", "viewer")
);

/** @satisfies {AuthUserValidator} */
const authUserShape = object({
  created_at: string,
  role: roleShape,
  user_id: string,
  username: string,
});

/** @satisfies {AuthTokenValidator} */
const authTokenShape = object({
  access_token: string,
  expires_at: string,
  token_type: optional(string),
  user: authUserShape,
});

/** @satisfies {AuthBootstrapStatusValidator} */
const authBootstrapStatusShape = object({
  bootstrap_admin_configured: boolean,
  bootstrap_token: nullish(string),
  bootstrap_token_warning: nullish(string),
  first_admin_available: boolean,
  has_users: boolean,
});

const authMeMetaShape = object({ auth_enabled: boolean });

/** @satisfies {AuthSetupReadinessValidator} */
const authSetupReadinessShape = object({
  background_worker_enabled: boolean,
  provider: string,
  provider_credential_configured: boolean,
  scheduler_enabled: boolean,
});

/** @satisfies {AuthInvitationValidator} */
const authInvitationShape = object({
  consumed_at: nullish(string),
  created_at: string,
  email: string,
  expires_at: string,
  invitation_id: string,
  invite_url: nullish(string),
  mailto_url: nullish(string),
  revoked_at: nullish(string),
  role: roleShape,
  status: string,
  warning: nullish(string),
});

/** @satisfies {DeviceTokenValidator} */
const deviceTokenShape = object({
  created_at: string,
  device_token_id: string,
  label: string,
  last_used_at: nullish(string),
  revoked_at: nullish(string),
});

/** @satisfies {DeviceEnrollmentValidator} */
const deviceEnrollmentShape = object({
  enrollment_id: string,
  enrollment_qr_svg: string,
  enrollment_url: string,
  expires_at: string,
  offer_token: string,
});

/** @satisfies {DeviceConsumeValidator} */
const deviceConsumeShape = object({
  created_at: string,
  device_token_id: string,
  label: string,
  secret: string,
});

/** @satisfies {PersonalAccessTokenValidator} */
const personalAccessTokenShape = object({
  created_at: string,
  expires_at: string,
  label: string,
  last_used_at: nullish(string),
  read_only: boolean,
  revoked_at: nullish(string),
  role: roleShape,
  scope: string,
  token_id: string,
});

/** @satisfies {PersonalAccessTokenIssuedValidator} */
const personalAccessTokenIssuedShape = object({
  created_at: string,
  expires_at: string,
  label: string,
  last_used_at: nullish(string),
  read_only: boolean,
  revoked_at: nullish(string),
  role: roleShape,
  scope: string,
  secret: string,
  token_id: string,
});

/** @param {string} path @param {Record<string, unknown>} body */
async function authenticate(path, body, options = {}) {
  const envelope = await apiFetch(path, { ...options, body, method: "POST" });
  return parseResource(envelope, authTokenShape);
}

async function getBootstrapStatus(options = {}) {
  const envelope = await apiFetch("/auth/bootstrap-status", options);
  return parseResource(envelope, authBootstrapStatusShape);
}

async function getCurrentUser(options = {}) {
  const envelope = await apiFetch("/auth/me", options);
  const result = parseResourceWithMeta(envelope, authUserShape, authMeMetaShape);
  return { authEnabled: result.meta.auth_enabled, user: result.data };
}

async function getSetupReadiness(options = {}) {
  const envelope = await apiFetch("/auth/setup-readiness", options);
  return parseResource(envelope, authSetupReadinessShape);
}

async function refreshSession(options = {}) {
  const envelope = await apiFetch("/auth/refresh", { ...options, method: "POST" });
  return parseResource(envelope, authTokenShape);
}

async function listUsers(options = {}) {
  const envelope = await apiFetch(buildApiPath("/auth/users", { limit: 200 }), options);
  return parseCollection(envelope, authUserShape);
}

/** @param {string} userId @param {Record<string, unknown>} body */
async function updateUser(userId, body, options = {}) {
  const envelope = await apiFetch(`/auth/users/${userId}`, {
    ...options,
    body,
    method: "PATCH",
  });
  return parseResource(envelope, authUserShape);
}

async function listInvitations(options = {}) {
  const envelope = await apiFetch(
    buildApiPath("/auth/invitations", { limit: 200 }),
    options
  );
  return parseCollection(envelope, authInvitationShape);
}

/** @param {Record<string, unknown>} body */
async function createInvitation(body, options = {}) {
  const envelope = await apiFetch("/auth/invitations", {
    ...options,
    body,
    method: "POST",
  });
  return parseResource(envelope, authInvitationShape);
}

/** @param {string} invitationId */
async function revokeInvitation(invitationId, options = {}) {
  const envelope = await apiFetch(`/auth/invitations/${invitationId}`, {
    ...options,
    method: "DELETE",
  });
  return parseResource(envelope, authInvitationShape);
}

async function listDevices(options = {}) {
  const envelope = await apiFetch("/auth/devices", options);
  return parseCollection(envelope, deviceTokenShape);
}

/** @param {Record<string, unknown>} body */
async function createDeviceEnrollment(body, options = {}) {
  const envelope = await apiFetch("/auth/devices/enrollment", {
    ...options,
    body,
    method: "POST",
  });
  return parseResource(envelope, deviceEnrollmentShape);
}

/** @param {Record<string, unknown>} body */
async function consumeDeviceEnrollment(body, options = {}) {
  const envelope = await apiFetch("/auth/devices/consume", {
    ...options,
    body,
    method: "POST",
  });
  return parseResource(envelope, deviceConsumeShape);
}

/** @param {string} deviceTokenId */
async function revokeDevice(deviceTokenId, options = {}) {
  const envelope = await apiFetch(`/auth/devices/${deviceTokenId}`, {
    ...options,
    method: "DELETE",
  });
  return parseResource(envelope, deviceTokenShape);
}

async function listPersonalAccessTokens(options = {}) {
  const envelope = await apiFetch("/auth/tokens", options);
  return parseCollection(envelope, personalAccessTokenShape);
}

/** @param {Record<string, unknown>} body */
async function createPersonalAccessToken(body, options = {}) {
  const envelope = await apiFetch("/auth/tokens", {
    ...options,
    body,
    method: "POST",
  });
  return parseResource(envelope, personalAccessTokenIssuedShape);
}

/** @param {string} tokenId */
async function revokePersonalAccessToken(tokenId, options = {}) {
  const envelope = await apiFetch(`/auth/tokens/${tokenId}`, {
    ...options,
    method: "DELETE",
  });
  return parseResource(envelope, personalAccessTokenShape);
}

export {
  authBootstrapStatusShape,
  authInvitationShape,
  authSetupReadinessShape,
  authTokenShape,
  authUserShape,
  authenticate,
  consumeDeviceEnrollment,
  createDeviceEnrollment,
  createInvitation,
  createPersonalAccessToken,
  deviceConsumeShape,
  deviceEnrollmentShape,
  deviceTokenShape,
  getBootstrapStatus,
  getCurrentUser,
  getSetupReadiness,
  listDevices,
  listInvitations,
  listPersonalAccessTokens,
  listUsers,
  personalAccessTokenIssuedShape,
  personalAccessTokenShape,
  refreshSession,
  revokeDevice,
  revokeInvitation,
  revokePersonalAccessToken,
  updateUser,
};
