"use strict";

const configStore = require("../config");
const { runOnboarding } = require("../onboarding");

const BACKEND_BASE_URL = "https://prophet-wwxjsbvhoa-uc.a.run.app";

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

async function requestJson(fetchImpl, path, payload, options = {}) {
  const method = options.method || "POST";
  const headers = { ...(options.headers || {}) };
  const request = { method, headers };
  if (payload !== undefined && payload !== null) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
    request.body = JSON.stringify(payload);
  }

  const response = await fetchImpl(`${BACKEND_BASE_URL}${path}`, request);
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
  const onboarding = options.runOnboarding || runOnboarding;

  const runOnboardingFlow = async () => onboarding({
    console: consoleLike,
    fetch: fetchImpl,
    config,
    prompts: options.prompts,
    stdin: options.stdin,
    stdout: options.stdout,
    backendBaseUrl: BACKEND_BASE_URL,
  });

  if (!config.configExists || !config.configExists()) {
    return runOnboardingFlow();
  }

  const existing = typeof config.readConfig === "function" ? config.readConfig() : null;
  if (!isConfigValid(config, existing)) {
    consoleLike.log("Warning: Profile config appears incomplete. Starting setup.");
    if (typeof config.clearConfig === "function") {
      config.clearConfig();
    }
    return runOnboardingFlow();
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
    if (error instanceof GatewayError && (error.status === 404 || error.status === 400)) {
      if (typeof config.clearConfig === "function") {
        config.clearConfig();
      }
      return runOnboardingFlow();
    }
    consoleLike.log("Warning: Prophet could not verify your saved profile. Continuing without profile sync.");
    return { status: "offline" };
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
  GatewayError,
  buildDeviceHeaders,
  ensureGatewayProfile,
  requestJson,
  sendChatMessage,
};
