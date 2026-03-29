"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { parseCommand, runCli } = require("../lib/runner");
const { formatWhatsAppResponse } = require("../lib/whatsapp_gateway/formatting");
const { createInboundMessageHandler, shouldProcessMessage } = require("../lib/whatsapp_gateway/inbound");
const { SentMessageTracker } = require("../lib/whatsapp_gateway/outbound");
const { startWhatsAppGateway } = require("../lib/whatsapp_gateway/runtime");
const { readWhatsAppState } = require("../lib/whatsapp_gateway/state");

function createConsole() {
  return {
    messages: [],
    log(message) {
      this.messages.push(String(message));
    },
  };
}

function createConfigStub(rootDir, initialConfig = null) {
  let config = initialConfig;
  return {
    configExists() {
      return Boolean(config);
    },
    readConfig() {
      return config;
    },
    writeConfig(next) {
      config = next;
      return true;
    },
    clearConfig() {
      config = null;
      return true;
    },
    isConfigValid(candidate) {
      return Boolean(candidate && candidate.device_token && candidate.onboarded === true);
    },
    getDeviceToken() {
      return this.isConfigValid(config) ? config.device_token : null;
    },
    getConfigDir() {
      return rootDir;
    },
  };
}

function withTempDir(t) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "prophetaf-whatsapp-"));
  t.after(() => {
    fs.rmSync(tempDir, { recursive: true, force: true });
  });
  return tempDir;
}

test("parseCommand recognizes whatsapp mode", () => {
  assert.deepEqual(parseCommand(["whatsapp"]), { command: "whatsapp" });
});

test("runCli dispatches whatsapp mode to the gateway runtime", async () => {
  const fakeConsole = createConsole();
  let receivedOptions = null;

  const exitCode = await runCli({
    argv: ["whatsapp"],
    console: fakeConsole,
    fetch: async () => {
      throw new Error("fetch should not be called directly in this test");
    },
    loadUpdateNotifier: async () => () => ({ update: null }),
    runWhatsAppGateway: async options => {
      receivedOptions = options;
      return 0;
    },
  });

  assert.equal(exitCode, 0);
  assert.ok(receivedOptions);
  assert.equal(typeof receivedOptions.fetch, "function");
});

test("formatWhatsAppResponse strips ansi and flattens structured command views", () => {
  const text = formatWhatsAppResponse({
    message: "\u001b[33mAvailable session commands:\u001b[0m",
    metadata: {
      view: "help_menu",
      commands: [
        ["/help", "List all available commands"],
        ["/memory", "Show current PROPHET.md contents"],
      ],
    },
  });

  assert.equal(
    text,
    [
      "Available session commands:",
      "",
      "/help - List all available commands",
      "/memory - Show current PROPHET.md contents",
    ].join("\n"),
  );
});

test("shouldProcessMessage ignores gateway replies, old history, and non-self chats", async () => {
  const tracker = new SentMessageTracker();
  tracker.remember("sent-1");

  assert.equal(await shouldProcessMessage({
    fromMe: true,
    id: { _serialized: "sent-1" },
    body: "ignored",
    timestamp: Math.floor(Date.now() / 1000),
    getChat: async () => ({ id: { _serialized: "me@c.us" } }),
  }, {
    ownChatId: "me@c.us",
    tracker,
    gatewayStartedAt: Date.now(),
  }), false);

  assert.equal(await shouldProcessMessage({
    fromMe: true,
    id: { _serialized: "old-1" },
    body: "older message",
    timestamp: 1,
    getChat: async () => ({ id: { _serialized: "me@c.us" } }),
  }, {
    ownChatId: "me@c.us",
    tracker: new SentMessageTracker(),
    gatewayStartedAt: Date.now(),
  }), false);

  assert.equal(await shouldProcessMessage({
    fromMe: true,
    id: { _serialized: "group-1" },
    body: "wrong chat",
    timestamp: Math.floor(Date.now() / 1000),
    getChat: async () => ({ id: { _serialized: "friend@c.us" } }),
  }, {
    ownChatId: "me@c.us",
    tracker: new SentMessageTracker(),
    gatewayStartedAt: Date.now() - 1000,
  }), false);
});

test("createInboundMessageHandler routes self chat messages, persists session id, and replies with plain text", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  const replies = [];
  const fetchCalls = [];
  const tracker = new SentMessageTracker();
  fs.mkdirSync(tempDir, { recursive: true });
  fs.writeFileSync(path.join(tempDir, "whatsapp-chat.json"), JSON.stringify({ session_id: "sess-1" }), "utf8");

  const handler = createInboundMessageHandler({
    config,
    tracker,
    gatewayStartedAt: Date.now() - 2000,
    console: createConsole(),
    getOwnChatId: () => "me@c.us",
    fetch: async (url, options) => {
      fetchCalls.push({ url, options });
      return {
        ok: true,
        status: 200,
        async text() {
          return JSON.stringify({
            session_id: "sess-2",
            message: "**Use** `/memory`",
            metadata: {
              view: "help_menu",
              commands: [["/memory", "Show current PROPHET.md contents"]],
            },
          });
        },
      };
    },
  });

  await handler({
    fromMe: true,
    body: "/help",
    timestamp: Math.floor(Date.now() / 1000),
    id: { _serialized: "msg-1" },
    getChat: async () => ({ id: { _serialized: "me@c.us" } }),
    async reply(text) {
      replies.push(text);
      return { id: { _serialized: "reply-1" } };
    },
  });

  assert.equal(fetchCalls.length, 1);
  assert.equal(JSON.parse(fetchCalls[0].options.body).session_id, "sess-1");
  assert.equal(fetchCalls[0].options.headers["X-Device-Token"], "device-123");
  assert.equal(replies[0], [
    "Use /memory",
    "",
    "/memory - Show current PROPHET.md contents",
  ].join("\n"));
  assert.equal(readWhatsAppState(config).session_id, "sess-2");
});

test("startWhatsAppGateway bootstraps onboarding before initializing the client", async () => {
  const fakeConsole = createConsole();
  const qrCalls = [];
  const registeredEvents = {};
  let onboardingCalls = 0;
  let initializeCalls = 0;
  let destroyCalls = 0;

  const exitCode = await startWhatsAppGateway({
    console: fakeConsole,
    config: createConfigStub(path.join(os.tmpdir(), "prophetaf-whatsapp-missing"), null),
    fetch: async (url) => {
      if (String(url).includes("/api/v1/profile")) {
        throw new Error("profile should not be checked before onboarding");
      }
      return {
        ok: true,
        status: 200,
        async text() {
          return "{}";
        },
      };
    },
    runOnboarding: async ({ config }) => {
      onboardingCalls += 1;
      config.writeConfig({
        device_token: "device-123",
        display_name: "Tafar",
        onboarded: true,
      });
      return { status: "completed" };
    },
    qrTerminal: {
      generate(value) {
        qrCalls.push(value);
      },
    },
    whatsappModule: {
      LocalAuth: class LocalAuth {},
    },
    createClient: () => ({
      info: { wid: { _serialized: "me@c.us" } },
      on(event, handler) {
        registeredEvents[event] = handler;
      },
      async initialize() {
        initializeCalls += 1;
        registeredEvents.qr("qr-token");
        await registeredEvents.ready();
      },
      async destroy() {
        destroyCalls += 1;
      },
    }),
    loginOnly: true,
  });

  assert.equal(exitCode, 0);
  assert.equal(onboardingCalls, 1);
  assert.equal(initializeCalls, 1);
  assert.equal(qrCalls[0], "qr-token");
  assert.equal(destroyCalls, 1);
  assert.ok(fakeConsole.messages.some(message => message.includes("Login complete")));
});
