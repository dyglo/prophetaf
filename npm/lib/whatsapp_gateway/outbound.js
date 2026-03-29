"use strict";

const { formatWhatsAppResponse } = require("./formatting");

class SentMessageTracker {
  constructor() {
    this.sentIds = new Map();
  }

  remember(messageId) {
    if (!messageId) {
      return;
    }
    this.sentIds.set(messageId, Date.now());
    this.prune();
  }

  has(messageId) {
    if (!messageId) {
      return false;
    }
    this.prune();
    return this.sentIds.has(messageId);
  }

  prune(maxAgeMs = 5 * 60 * 1000) {
    const now = Date.now();
    for (const [messageId, timestamp] of this.sentIds.entries()) {
      if (now - timestamp > maxAgeMs) {
        this.sentIds.delete(messageId);
      }
    }
  }
}

function messageIdentifier(message) {
  return message
    && message.id
    && typeof message.id._serialized === "string"
    ? message.id._serialized
    : "";
}

async function sendReplyToWhatsApp(message, response, tracker) {
  const text = formatWhatsAppResponse(response);
  if (!text) {
    return null;
  }
  const sent = await message.reply(text);
  if (tracker) {
    tracker.remember(messageIdentifier(sent));
  }
  return sent;
}

module.exports = {
  SentMessageTracker,
  messageIdentifier,
  sendReplyToWhatsApp,
};
