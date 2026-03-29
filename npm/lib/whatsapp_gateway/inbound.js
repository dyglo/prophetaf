"use strict";

const { sendChatMessage } = require("./backend");
const { sendReplyToWhatsApp, messageIdentifier } = require("./outbound");
const { readWhatsAppState, updateWhatsAppState } = require("./state");

function messageTimestampMs(message) {
  const value = Number(message && message.timestamp);
  return Number.isFinite(value) && value > 0 ? value * 1000 : 0;
}

async function resolveChatId(message) {
  if (!message || typeof message.getChat !== "function") {
    return message && (message.to || message.from) ? (message.to || message.from) : "";
  }
  try {
    const chat = await message.getChat();
    if (chat && chat.id && typeof chat.id._serialized === "string") {
      return chat.id._serialized;
    }
  } catch {
    // Fall back to transport metadata.
  }
  return message && (message.to || message.from) ? (message.to || message.from) : "";
}

async function shouldProcessMessage(message, options = {}) {
  const ownChatId = options.ownChatId || "";
  const tracker = options.tracker;
  const gatewayStartedAt = Number(options.gatewayStartedAt || 0);

  if (!message || message.fromMe !== true) {
    return false;
  }
  if (tracker && tracker.has(messageIdentifier(message))) {
    return false;
  }
  if (gatewayStartedAt > 0) {
    const timestamp = messageTimestampMs(message);
    if (timestamp > 0 && timestamp < gatewayStartedAt) {
      return false;
    }
  }

  const body = typeof message.body === "string" ? message.body.trim() : "";
  if (!body) {
    return false;
  }

  const chatId = await resolveChatId(message);
  return Boolean(ownChatId && chatId === ownChatId);
}

function createInboundMessageHandler(options = {}) {
  const fetchImpl = options.fetch || global.fetch;
  const consoleLike = options.console || global.console;
  const config = options.config;
  const tracker = options.tracker;
  const gatewayStartedAt = Number(options.gatewayStartedAt || Date.now());
  const getOwnChatId = typeof options.getOwnChatId === "function"
    ? options.getOwnChatId
    : () => options.ownChatId || "";

  return async message => {
    const ownChatId = await Promise.resolve(getOwnChatId());
    const allowed = await shouldProcessMessage(message, {
      ownChatId,
      tracker,
      gatewayStartedAt,
    });
    if (!allowed) {
      return null;
    }

    const body = message.body.trim();
    const currentState = readWhatsAppState(config);
    const response = await sendChatMessage(fetchImpl, body, currentState.session_id || null, { config });
    updateWhatsAppState({ session_id: response && response.session_id ? response.session_id : null }, config);
    const sent = await sendReplyToWhatsApp(message, response, tracker);
    const sessionId = response && response.session_id ? response.session_id : "new";
    consoleLike.log(`WhatsApp reply sent for session ${sessionId}.`);
    return { response, sent };
  };
}

module.exports = {
  createInboundMessageHandler,
  messageTimestampMs,
  resolveChatId,
  shouldProcessMessage,
};
