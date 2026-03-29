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

function readWhatsAppState(config = configStore) {
  try {
    const raw = fs.readFileSync(getWhatsAppStatePath(config), "utf8");
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeWhatsAppState(nextState, config = configStore) {
  fs.mkdirSync(getGatewayRoot(config), { recursive: true });
  fs.writeFileSync(
    getWhatsAppStatePath(config),
    JSON.stringify(nextState || {}, null, 2),
    "utf8",
  );
}

function updateWhatsAppState(patch, config = configStore) {
  const current = readWhatsAppState(config);
  const nextState = { ...current, ...(patch || {}) };
  writeWhatsAppState(nextState, config);
  return nextState;
}

module.exports = {
  getGatewayRoot,
  getWhatsAppSessionDir,
  getWhatsAppStatePath,
  readWhatsAppState,
  updateWhatsAppState,
  writeWhatsAppState,
};
