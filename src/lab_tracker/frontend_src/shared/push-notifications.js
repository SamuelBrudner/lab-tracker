import { apiRequest } from "./api.js";

function supportsPushNotifications() {
  return (
    typeof navigator !== "undefined" &&
    Boolean(navigator.serviceWorker) &&
    typeof globalThis.Notification !== "undefined" &&
    typeof globalThis.PushManager !== "undefined"
  );
}

function secureContextAvailable() {
  return globalThis.isSecureContext !== false;
}

function urlBase64ToUint8Array(value) {
  const normalized = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = globalThis.atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function getPushRegistration() {
  if (!navigator.serviceWorker?.ready) {
    return null;
  }
  return navigator.serviceWorker.ready;
}

function serializePushSubscription(subscription) {
  const serialized = subscription.toJSON();
  return {
    endpoint: serialized.endpoint || subscription.endpoint,
    expiration_time_ms:
      serialized.expirationTime ?? subscription.expirationTime ?? null,
    keys: serialized.keys || {},
  };
}

async function bindPushSubscription({ subscription, token }) {
  await apiRequest("/notifications/push/subscriptions", {
    body: serializePushSubscription(subscription),
    method: "POST",
    token,
  });
}

async function rebindExistingPushSubscription({ token }) {
  if (!supportsPushNotifications() || !secureContextAvailable()) {
    return false;
  }
  const configuration = await apiRequest("/notifications/push/config", { token });
  if (!configuration?.enabled) {
    return false;
  }
  const registration = await getPushRegistration();
  const subscription = await registration?.pushManager?.getSubscription?.();
  if (!subscription) {
    return false;
  }
  await bindPushSubscription({ subscription, token });
  return true;
}

async function getPushNotificationState({ token }) {
  const configuration = await apiRequest("/notifications/push/config", { token });
  const supported = supportsPushNotifications();
  const secure = secureContextAvailable();
  if (!supported || !secure || !configuration?.enabled) {
    return {
      configured: Boolean(configuration?.enabled),
      permission: globalThis.Notification?.permission || "unavailable",
      secure,
      subscribed: false,
      supported,
    };
  }
  const registration = await getPushRegistration();
  const subscription = await registration?.pushManager?.getSubscription?.();
  if (subscription) {
    await bindPushSubscription({ subscription, token });
  }
  return {
    configured: true,
    permission: globalThis.Notification.permission,
    secure: true,
    subscribed: Boolean(subscription),
    supported: true,
  };
}

async function enablePushNotifications({ token }) {
  const configuration = await apiRequest("/notifications/push/config", { token });
  if (!configuration?.enabled || !configuration.application_server_key) {
    throw new Error("Review notifications are not configured on this Lab Tracker instance.");
  }
  if (!supportsPushNotifications()) {
    throw new Error("This browser does not support Web Push notifications.");
  }
  if (!secureContextAvailable()) {
    throw new Error("Review notifications require HTTPS or localhost.");
  }
  const permission = await globalThis.Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notification permission was not granted.");
  }
  const registration = await getPushRegistration();
  if (!registration?.pushManager) {
    throw new Error("The Lab Tracker service worker is not ready.");
  }
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      applicationServerKey: urlBase64ToUint8Array(
        configuration.application_server_key
      ),
      userVisibleOnly: true,
    });
  }
  await bindPushSubscription({ subscription, token });
  return subscription;
}

async function disablePushNotifications({ token, bestEffort = false }) {
  if (!navigator.serviceWorker?.ready) {
    return false;
  }
  const registration = await getPushRegistration();
  const subscription = await registration?.pushManager?.getSubscription?.();
  if (!subscription) {
    return false;
  }
  let serverError = null;
  try {
    await apiRequest("/notifications/push/unsubscribe", {
      body: { endpoint: subscription.endpoint },
      method: "POST",
      notifyAuthRejected: false,
      token,
    });
  } catch (error) {
    serverError = error;
  }
  await subscription.unsubscribe();
  if (serverError && !bestEffort) {
    throw serverError;
  }
  return true;
}

export {
  disablePushNotifications,
  enablePushNotifications,
  getPushNotificationState,
  rebindExistingPushSubscription,
  supportsPushNotifications,
  urlBase64ToUint8Array,
};
