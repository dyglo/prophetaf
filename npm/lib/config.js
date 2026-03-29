"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function getConfigDir() {
  const override = process.env.PROPHET_CONFIG_DIR;
  if (typeof override === "string" && override.trim().length > 0) {
    return override.trim();
  }
  return path.join(os.homedir(), ".prophet");
}

function getConfigPath() {
  return path.join(getConfigDir(), "config.json");
}

function configExists() {
  try {
    return fs.existsSync(getConfigPath());
  } catch {
    return false;
  }
}

function isConfigValid(config) {
  return Boolean(
    config
    && typeof config === "object"
    && typeof config.device_token === "string"
    && config.device_token.trim().length > 0
    && config.onboarded === true,
  );
}

function readConfig() {
  try {
    if (!configExists()) {
      return null;
    }
    const raw = fs.readFileSync(getConfigPath(), "utf8");
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function writeConfig(data) {
  const configDir = getConfigDir();
  const configPath = getConfigPath();
  try {
    fs.mkdirSync(configDir, { recursive: true });
    fs.writeFileSync(configPath, JSON.stringify(data, null, 2), "utf8");
    return true;
  } catch {
    return false;
  }
}

function clearConfig() {
  try {
    if (!configExists()) {
      return true;
    }
    fs.unlinkSync(getConfigPath());
    return true;
  } catch {
    return false;
  }
}

function getDeviceToken() {
  const config = readConfig();
  return isConfigValid(config) ? config.device_token : null;
}

module.exports = {
  clearConfig,
  configExists,
  getConfigDir,
  getConfigPath,
  getDeviceToken,
  isConfigValid,
  readConfig,
  writeConfig,
};
