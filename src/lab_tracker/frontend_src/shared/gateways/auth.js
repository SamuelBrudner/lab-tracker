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

/** @typedef {import("../../generated/openapi.js").components["schemas"]["AuthBootstrapStatus"]} AuthBootstrapStatus */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["AuthInvitationRead"]} AuthInvitationRead */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["AuthTokenRead"]} AuthTokenRead */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["AuthUserRead"]} AuthUserRead */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["DeviceConsumeRead"]} DeviceConsumeRead */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["DeviceEnrollmentRead"]} DeviceEnrollmentRead */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["DeviceTokenRead"]} DeviceTokenRead */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["PersonalAccessTokenIssuedRead"]} PersonalAccessTokenIssuedRead */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["PersonalAccessTokenRead"]} PersonalAccessTokenRead */
/** @typedef {import("../contract.js").Validator<AuthBootstrapStatus>} AuthBootstrapStatusValidator */
/** @typedef {import("../contract.js").Validator<AuthInvitationRead>} AuthInvitationValidator */
/** @typedef {import("../contract.js").Validator<AuthTokenRead>} AuthTokenValidator */
/** @typedef {import("../contract.js").Validator<AuthUserRead>} AuthUserValidator */
/** @typedef {import("../contract.js").Validator<DeviceConsumeRead>} DeviceConsumeValidator */
/** @typedef {import("../contract.js").Validator<DeviceEnrollmentRead>} DeviceEnrollmentValidator */
/** @typedef {import("../contract.js").Validator<DeviceTokenRead>} DeviceTokenValidator */
/** @typedef {import("../contract.js").Validator<PersonalAccessTokenIssuedRead>} PersonalAccessTokenIssuedValidator */
/** @typedef {import("../contract.js").Validator<PersonalAccessTokenRead>} PersonalAccessTokenValidator */

const roleShape = oneOf("admin", "editor", "viewer");

/** @type {AuthUserValidator} */
const authUserShape = /** @type {AuthUserValidator} */ (
  object({
    created_at: string,
    role: roleShape,
    user_id: string,
    username: string,
  })
);

/** @type {AuthTokenValidator} */
const authTokenShape = /** @type {AuthTokenValidator} */ (
  object({
    access_token: string,
    expires_at: string,
    token_type: optional(string),
    user: authUserShape,
  })
);

/** @type {AuthBootstrapStatusValidator} */
const authBootstrapStatusShape = /** @type {AuthBootstrapStatusValidator} */ (
  object({
    bootstrap_admin_configured: boolean,
    bootstrap_token: nullish(string),
    bootstrap_token_warning: nullish(string),
    first_admin_available: boolean,
    has_users: boolean,
  })
);

const authMeMetaShape = object({ auth_enabled: boolean });

/** @type {AuthInvitationValidator} */
const authInvitationShape = /** @type {AuthInvitationValidator} */ (
  object({
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
  })
);

/** @type {DeviceTokenValidator} */
const deviceTokenShape = /** @type {DeviceTokenValidator} */ (
  object({
    created_at: string,
    device_token_id: string,
    label: string,
    last_used_at: nullish(string),
    revoked_at: nullish(string),
  })
);

/** @type {DeviceEnrollmentValidator} */
const deviceEnrollmentShape = /** @type {DeviceEnrollmentValidator} */ (
  object({
    enrollment_id: string,
    enrollment_qr_svg: string,
    enrollment_url: string,
    expires_at: string,
    offer_token: string,
  })
);

/** @type {DeviceConsumeValidator} */
const deviceConsumeShape = /** @type {DeviceConsumeValidator} */ (
  object({
    created_at: string,
    device_token_id: string,
    label: string,
    secret: string,
  })
);

/** @type {PersonalAccessTokenValidator} */
const personalAccessTokenShape = /** @type {PersonalAccessTokenValidator} */ (
  object({
    created_at: string,
    expires_at: string,
    label: string,
    last_used_at: nullish(string),
    read_only: boolean,
    revoked_at: nullish(string),
    role: roleShape,
    token_id: string,
  })
);

/** @type {PersonalAccessTokenIssuedValidator} */
const personalAccessTokenIssuedShape = /** @type {PersonalAccessTokenIssuedValidator} */ (
  object({
    created_at: string,
    expires_at: string,
    label: string,
    last_used_at: nullish(string),
    read_only: boolean,
    revoked_at: nullish(string),
    role: roleShape,
    secret: string,
    token_id: string,
  })
);

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
