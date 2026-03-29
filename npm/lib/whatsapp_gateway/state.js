"use strict";

const fs = require("node:fs");
const path = require("node:path");
const configStore = require("../config");

function getGatewayRoot(config = configStore) {
  return config && typeof config.getConfigDir === "function"
    ? config.getConfigDir()
    : path.join(require("node:os").homedir(), ".prophet");
}

function getWhatsAppSessionDir(config = configStore) {
  return path.join(getGatewayRoot(config), "whatsapp-session");
}

function getWhatsAppStatePath(config = configStore) {
  return path.join(getGatewayRoot(config), "whatsapp-chat.json");
}

function getWhatsAppDaemonPidPath(config = configStore) {
  return path.join(getGatewayRoot(config), "whatsapp-daemon.pid");
}

function getWhatsAppDaemonLogPath(config = configStore) {
  return path.join(getGatewayRoot(config), "whatsapp-daemon.log");
}

function getWhatsAppDaemonStatePath(config = configStore) {
  return path.join(getGatewayRoot(config), "whatsapp-daemon.json");
}

function readJsonFile(filePath, fallbackValue) {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : fallbackValue;
  } catch {
    return fallbackValue;
  }
}

function writeJsonFile(filePath, value, config = configStore) {
  fs.mkdirSync(getGatewayRoot(config), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(value || {}, null, 2), "utf8");
}

function readWhatsAppState(config = configStore) {
  return readJsonFile(getWhatsAppStatePath(config), {});
}

function writeWhatsAppState(nextState, config = configStore) {
  writeJsonFile(getWhatsAppStatePath(config), nextState || {}, config);
}

function updateWhatsAppState(patch, config = configStore) {
  const current = readWhatsAppState(config);
  const nextState = { ...current, ...(patch || {}) };
  writeWhatsAppState(nextState, config);
  return nextState;
}

function readWhatsAppDaemonPid(config = configStore) {
  try {
    const raw = fs.readFileSync(getWhatsAppDaemonPidPath(config), "utf8").trim();
    const pid = Number.parseInt(raw, 10);
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

function writeWhatsAppDaemonPid(pid, config = configStore) {
  fs.mkdirSync(getGatewayRoot(config), { recursive: true });
  fs.writeFileSync(getWhatsAppDaemonPidPath(config), `${pid}\n`, "utf8");
}

function readWhatsAppDaemonState(config = configStore) {
  return readJsonFile(getWhatsAppDaemonStatePath(config), {});
}

function writeWhatsAppDaemonState(nextState, config = configStore) {
  writeJsonFile(getWhatsAppDaemonStatePath(config), nextState || {}, config);
}

function updateWhatsAppDaemonState(patch, config = configStore) {
  const current = readWhatsAppDaemonState(config);
  const nextState = { ...current, ...(patch || {}) };
  writeWhatsAppDaemonState(nextState, config);
  return nextState;
}

function removeFileIfExists(filePath) {
  try {
    fs.unlinkSync(filePath);
  } catch {
    // Ignore missing file cleanup.
  }
}

function clearWhatsAppDaemonFiles(config = configStore) {
  removeFileIfExists(getWhatsAppDaemonPidPath(config));
  removeFileIfExists(getWhatsAppDaemonStatePath(config));
}

module.exports = {
  clearWhatsAppDaemonFiles,
  getGatewayRoot,
  getWhatsAppDaemonLogPath,
  getWhatsAppDaemonPidPath,
  getWhatsAppDaemonStatePath,
  getWhatsAppSessionDir,
  getWhatsAppStatePath,
  readWhatsAppDaemonPid,
  readWhatsAppDaemonState,
  readWhatsAppState,
  updateWhatsAppDaemonState,
  updateWhatsAppState,
  writeWhatsAppDaemonPid,
  writeWhatsAppDaemonState,
  writeWhatsAppState,
};
