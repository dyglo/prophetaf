"use strict";

const configStore = require("../config");

const BACKEND_BASE_URL = "https://prophet-wwxjsbvhoa-uc.a.run.app";
const WHATSAPP_SETUP_REQUIRED_MESSAGE = "WhatsApp requires an existing Prophet profile. Run prophetaf once in the terminal to finish setup, then run prophetaf whatsapp again.";
const FETCH_TIMEOUT_MS = 60_000;
const FETCH_RETRY_COUNT = 1;

class GatewayError extends Error {
  constructor(message, exitCode = 1) {
    super(message);
    this.name = "GatewayError";
    this.exitCode = exitCode;
  }
}

function buildDeviceHeaders(configModule = configStore) {
  const token = configModule && typeof configModule.getDeviceToken === "function"
    ? configModule.getDeviceToken()
    : null;
  return token ? { "X-Device-Token": token } : {};
}

function createFetchTimeoutError(timeoutMs) {
  const error = new Error(`fetch timed out after ${timeoutMs}ms`);
  error.name = "FetchTimeoutError";
  error.code = "FETCH_TIMEOUT";
  return error;
}

function isAbortError(error) {
  if (!error) {
    return false;
  }
  return error.name === "AbortError" || error.code === "ABORT_ERR";
}

function isRetryableFetchError(error) {
  if (!error) {
    return false;
  }
  if (error instanceof GatewayError && Number.isInteger(error.status)) {
    return false;
  }
  if (error.code === "FETCH_TIMEOUT") {
    return true;
  }
  if (isAbortError(error)) {
    return true;
  }
  return error instanceof Error;
}

async function fetchWithTimeout(fetchImpl, url, request, options = {}) {
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : FETCH_TIMEOUT_MS;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  let timeoutId = null;
  const finalRequest = { ...(request || {}) };

  if (controller) {
    finalRequest.signal = controller.signal;
    timeoutId = setTimeout(() => {
      controller.abort(createFetchTimeoutError(timeoutMs));
    }, timeoutMs);
  }

  try {
    return await fetchImpl(url, finalRequest);
  } catch (error) {
    if (isAbortError(error) && controller && controller.signal && controller.signal.aborted) {
      throw controller.signal.reason || createFetchTimeoutError(timeoutMs);
    }
    throw error;
  } finally {
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
    }
  }
}

async function fetchWithRetry(fetchImpl, url, request, options = {}) {
  const retryCount = Number.isInteger(options.retryCount) ? options.retryCount : FETCH_RETRY_COUNT;
  let lastError = null;

  for (let attempt = 0; attempt <= retryCount; attempt += 1) {
    try {
      return await fetchWithTimeout(fetchImpl, url, request, options);
    } catch (error) {
      lastError = error;
      if (attempt >= retryCount || !isRetryableFetchError(error)) {
        throw error;
      }
    }
  }

  throw lastError || new Error("fetch failed");
}

async function requestJson(fetchImpl, path, payload, options = {}) {
  const method = options.method || "POST";
  const headers = { ...(options.headers || {}) };
  const request = { method, headers };
  if (payload !== undefined && payload !== null) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
    request.body = JSON.stringify(payload);
  }

  const response = await fetchWithRetry(fetchImpl, `${BACKEND_BASE_URL}${path}`, request, {
    timeoutMs: options.timeoutMs,
    retryCount: options.retryCount,
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!response.ok) {
    const detail = typeof data === "string" ? data : JSON.stringify(data);
    const error = new GatewayError(`Backend request failed (${response.status}): ${detail}`);
    error.status = response.status;
    throw error;
  }

  return data;
}

function isConfigValid(configModule, candidate) {
  return typeof configModule.isConfigValid === "function"
    ? configModule.isConfigValid(candidate)
    : Boolean(candidate && candidate.device_token && candidate.onboarded === true);
}

async function ensureGatewayProfile(fetchImpl, consoleLike, options = {}) {
  const config = options.config || configStore;

  if (!config.configExists || !config.configExists()) {
    throw new GatewayError(WHATSAPP_SETUP_REQUIRED_MESSAGE);
  }

  const existing = typeof config.readConfig === "function" ? config.readConfig() : null;
  if (!isConfigValid(config, existing)) {
    throw new GatewayError(WHATSAPP_SETUP_REQUIRED_MESSAGE);
  }

  try {
    const profile = await requestJson(fetchImpl, "/api/v1/profile", null, {
      method: "GET",
      headers: {
        Accept: "application/json",
        ...buildDeviceHeaders(config),
      },
    });
    consoleLike.log(`Prophet profile ready for WhatsApp: ${profile.display_name}`);
    return { status: "loaded", profile };
  } catch (error) {
    const displayName = typeof existing.display_name === "string" && existing.display_name.trim()
      ? existing.display_name.trim()
      : "Trader";
    consoleLike.log(`Prophet profile ready for WhatsApp: ${displayName}`);
    return { status: "offline", profile: existing };
  }
}

async function sendChatMessage(fetchImpl, message, sessionId, options = {}) {
  const config = options.config || configStore;
  return requestJson(fetchImpl, "/chat", {
    message,
    session_id: sessionId || null,
    stream: false,
  }, {
    headers: {
      Accept: "application/json",
      ...buildDeviceHeaders(config),
    },
  });
}

module.exports = {
  BACKEND_BASE_URL,
  FETCH_RETRY_COUNT,
  FETCH_TIMEOUT_MS,
  GatewayError,
  WHATSAPP_SETUP_REQUIRED_MESSAGE,
  buildDeviceHeaders,
  ensureGatewayProfile,
  fetchWithRetry,
  fetchWithTimeout,
  requestJson,
  sendChatMessage,
};
