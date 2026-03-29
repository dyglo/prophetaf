"use strict";

const fs = require("node:fs");
const { ensureGatewayProfile, GatewayError } = require("./backend");
const { createInboundMessageHandler } = require("./inbound");
const { SentMessageTracker } = require("./outbound");
const { getWhatsAppSessionDir } = require("./state");

function createDefaultClientFactory(whatsappModule) {
  return options => new whatsappModule.Client(options);
}

function createClientOptions(whatsappModule, config) {
  const sessionDir = getWhatsAppSessionDir(config);
  fs.mkdirSync(sessionDir, { recursive: true });
  return {
    authStrategy: new whatsappModule.LocalAuth({
      clientId: "prophet-whatsapp",
      dataPath: sessionDir,
    }),
    puppeteer: {
      headless: true,
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
      ],
    },
  };
}

async function startWhatsAppGateway(options = {}) {
  const fetchImpl = options.fetch || global.fetch;
  const consoleLike = options.console || global.console;
  const config = options.config;
  const whatsappModule = options.whatsappModule || require("whatsapp-web.js");
  const qrTerminal = options.qrTerminal || require("qrcode-terminal");
  const loginOnly = options.loginOnly === true;

  if (typeof fetchImpl !== "function") {
    throw new GatewayError("This runtime does not provide fetch. Use Node.js 18 or newer.");
  }

  const profileState = await ensureGatewayProfile(fetchImpl, consoleLike, options);
  if (profileState && profileState.status === "cancelled") {
    return 0;
  }
  if (profileState && profileState.status === "failed") {
    return 1;
  }

  const createClient = options.createClient || createDefaultClientFactory(whatsappModule);
  const tracker = options.tracker || new SentMessageTracker();
  const gatewayStartedAt = Number(options.gatewayStartedAt || Date.now());
  const client = createClient(createClientOptions(whatsappModule, config));

  let ownChatId = "";
  const handleInboundMessage = createInboundMessageHandler({
    ...options,
    fetch: fetchImpl,
    console: consoleLike,
    config,
    tracker,
    gatewayStartedAt,
    getOwnChatId: () => ownChatId,
  });

  client.on("qr", qr => {
    consoleLike.log("Scan this WhatsApp QR code with your phone:");
    if (qrTerminal && typeof qrTerminal.generate === "function") {
      qrTerminal.generate(qr, { small: true });
    } else {
      consoleLike.log(qr);
    }
  });

  client.on("authenticated", () => {
    consoleLike.log("WhatsApp authenticated.");
  });

  client.on("auth_failure", message => {
    consoleLike.log(`WhatsApp authentication failed: ${message || "unknown error"}`);
  });

  client.on("ready", async () => {
    ownChatId = client.info
      && client.info.wid
      && typeof client.info.wid._serialized === "string"
      ? client.info.wid._serialized
      : "";
    consoleLike.log("WhatsApp gateway connected.");
    if (loginOnly) {
      consoleLike.log("Login complete. You can now run prophetaf whatsapp.");
      if (typeof client.destroy === "function") {
        await client.destroy();
      }
    }
  });

  client.on("disconnected", reason => {
    consoleLike.log(`WhatsApp gateway disconnected: ${reason || "unknown reason"}`);
  });

  client.on("message_create", async message => {
    try {
      await handleInboundMessage(message);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      consoleLike.log(`WhatsApp message handling failed: ${detail}`);
    }
  });

  await client.initialize();
  return 0;
}

module.exports = {
  createClientOptions,
  startWhatsAppGateway,
};
