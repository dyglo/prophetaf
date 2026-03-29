"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const childProcess = require("node:child_process");

const { parseCommand, runCli } = require("../lib/runner");
const {
  ensureGatewayProfile,
  requestJson,
  WHATSAPP_SETUP_REQUIRED_MESSAGE,
} = require("../lib/whatsapp_gateway/backend");
const { formatWhatsAppResponse } = require("../lib/whatsapp_gateway/formatting");
const { createInboundMessageHandler, shouldProcessMessage } = require("../lib/whatsapp_gateway/inbound");
const { SentMessageTracker } = require("../lib/whatsapp_gateway/outbound");
const {
  checkLinuxChromiumDependencies,
  createClientOptions,
  LINUX_CHROMIUM_INSTALL_COMMAND,
  printWhatsAppDaemonStatus,
  startWhatsAppDaemon,
  startWhatsAppGateway,
  stopWhatsAppDaemon,
} = require("../lib/whatsapp_gateway/runtime");
const {
  getWhatsAppDaemonLogPath,
  getWhatsAppDaemonPidPath,
  getWhatsAppDaemonStatePath,
  getWhatsAppSessionDir,
  readWhatsAppState,
} = require("../lib/whatsapp_gateway/state");

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
  assert.deepEqual(parseCommand(["whatsapp"]), { command: "whatsapp", mode: "foreground" });
  assert.deepEqual(parseCommand(["whatsapp", "--daemon"]), { command: "whatsapp", mode: "daemon" });
  assert.deepEqual(parseCommand(["whatsapp", "--stop"]), { command: "whatsapp", mode: "stop" });
  assert.deepEqual(parseCommand(["whatsapp", "--status"]), { command: "whatsapp", mode: "status" });
});

test("parseCommand still rejects missing values for non-boolean flags", () => {
  assert.throws(() => parseCommand(["risk", "--pair", "XAUUSD", "--sl", "--risk", "1"]), /Missing value for --sl/);
  assert.throws(() => parseCommand(["scan", "--pair"]), /Missing value for --pair/);
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
  assert.equal(receivedOptions.mode, "foreground");
});

test("runCli dispatches whatsapp daemon mode to the gateway runtime", async () => {
  let receivedOptions = null;

  const exitCode = await runCli({
    argv: ["whatsapp", "--daemon"],
    console: createConsole(),
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
  assert.equal(receivedOptions.mode, "daemon");
});

test("createClientOptions includes the Linux-safe Chromium launch args", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  const options = createClientOptions({
    LocalAuth: class LocalAuth {
      constructor(init) {
        Object.assign(this, init);
      }
    },
  }, config);

  assert.ok(options.puppeteer.args.includes("--no-sandbox"));
  assert.ok(options.puppeteer.args.includes("--disable-setuid-sandbox"));
  assert.ok(options.puppeteer.args.includes("--disable-dev-shm-usage"));
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

test("createInboundMessageHandler treats empty backend bodies as a non-throwing new session reply", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  const fakeConsole = createConsole();
  const tracker = new SentMessageTracker();
  const replies = [];

  const handler = createInboundMessageHandler({
    config,
    tracker,
    gatewayStartedAt: Date.now() - 2000,
    console: fakeConsole,
    getOwnChatId: () => "me@c.us",
    fetch: async () => ({
      ok: true,
      status: 200,
      async text() {
        return "";
      },
    }),
  });

  const result = await handler({
    fromMe: true,
    body: "hello",
    timestamp: Math.floor(Date.now() / 1000),
    id: { _serialized: "msg-empty" },
    getChat: async () => ({ id: { _serialized: "me@c.us" } }),
    async reply(text) {
      replies.push(text);
      return { id: { _serialized: "reply-empty" } };
    },
  });

  assert.deepEqual(result, { response: null, sent: null });
  assert.equal(readWhatsAppState(config).session_id, null);
  assert.equal(replies.length, 0);
  assert.ok(fakeConsole.messages.some(message => message.includes("session new")));
});

test("createInboundMessageHandler ignores malformed event payloads without throwing", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  let fetchCalls = 0;
  const handler = createInboundMessageHandler({
    config,
    gatewayStartedAt: Date.now() - 1000,
    console: createConsole(),
    getOwnChatId: () => "me@c.us",
    fetch: async () => {
      fetchCalls += 1;
      throw new Error("fetch should not run");
    },
  });

  await assert.doesNotReject(async () => {
    const result = await handler(undefined);
    assert.equal(result, null);
  });
  assert.equal(fetchCalls, 0);
});

test("ensureGatewayProfile fails clearly when the trader has not completed CLI setup", async () => {
  await assert.rejects(
    () => ensureGatewayProfile(async () => {
      throw new Error("fetch should not run");
    }, createConsole(), {
      config: createConfigStub(path.join(os.tmpdir(), "prophetaf-whatsapp-missing"), null),
    }),
    error => {
      assert.match(error.message, new RegExp(WHATSAPP_SETUP_REQUIRED_MESSAGE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
      return true;
    },
  );
});

test("ensureGatewayProfile falls back to the saved local profile when backend verification rejects it", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  const fakeConsole = createConsole();

  const profile = await ensureGatewayProfile(async () => ({
      ok: false,
      status: 404,
      async text() {
        return JSON.stringify({ detail: "Profile not found" });
      },
    }), fakeConsole, { config });

  assert.equal(profile.status, "offline");
  assert.equal(profile.profile.display_name, "Tafar");
  assert.ok(fakeConsole.messages.some(message => message.includes("Prophet profile ready for WhatsApp: Tafar")));
});

test("ensureGatewayProfile stays silent when profile verification fails offline", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  const fakeConsole = createConsole();

  const profile = await ensureGatewayProfile(async () => {
    throw new Error("network offline");
  }, fakeConsole, { config });

  assert.equal(profile.status, "offline");
  assert.equal(profile.profile.display_name, "Tafar");
  assert.ok(!fakeConsole.messages.some(message => /could not verify your saved profile/i.test(message)));
});

test("requestJson retries once for retryable network failures", async () => {
  let calls = 0;

  const response = await requestJson(async () => {
    calls += 1;
    if (calls === 1) {
      throw new Error("fetch failed");
    }
    return {
      ok: true,
      status: 200,
      async text() {
        return JSON.stringify({ ok: true });
      },
    };
  }, "/chat", { message: "hi" }, {});

  assert.equal(calls, 2);
  assert.deepEqual(response, { ok: true });
});

test("checkLinuxChromiumDependencies reports missing shared libraries clearly", () => {
  const result = checkLinuxChromiumDependencies({
    platform: "linux",
    getChromiumExecutablePath: () => "/tmp/chrome",
    execFileSync() {
      return [
        "libnspr4.so => not found",
        "libnss3.so => not found",
      ].join("\n");
    },
  });

  assert.equal(result.ok, false);
  assert.deepEqual(result.missingLibraries, ["libnspr4.so", "libnss3.so"]);
  assert.equal(result.installCommand, LINUX_CHROMIUM_INSTALL_COMMAND);
});

test("startWhatsAppGateway fails before initialization when Linux Chromium dependencies are missing", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  let initializeCalls = 0;

  await assert.rejects(
    () => startWhatsAppGateway({
      console: createConsole(),
      config,
      fetch: async (url) => {
        if (String(url).includes("/api/v1/profile")) {
          return {
            ok: true,
            status: 200,
            async text() {
              return JSON.stringify({ display_name: "Tafar" });
            },
          };
        }
        throw new Error(`Unexpected URL: ${url}`);
      },
      platform: "linux",
      getChromiumExecutablePath: () => "/tmp/chrome",
      execFileSync() {
        return "libnspr4.so => not found";
      },
      whatsappModule: {
        LocalAuth: class LocalAuth {},
      },
      qrTerminal: {
        generate() {},
      },
      createClient: () => ({
        on() {},
        async initialize() {
          initializeCalls += 1;
        },
      }),
    }),
    error => {
      assert.match(error.message, /missing required Linux shared libraries/i);
      assert.match(error.message, /sudo apt-get install -y libnspr4/i);
      return true;
    },
  );

  assert.equal(initializeCalls, 0);
});

test("startWhatsAppGateway initializes directly when a saved profile exists", async t => {
  const fakeConsole = createConsole();
  const qrCalls = [];
  const registeredEvents = {};
  let initializeCalls = 0;
  let destroyCalls = 0;
  const tempDir = withTempDir(t);

  const exitCode = await startWhatsAppGateway({
    console: fakeConsole,
    config: createConfigStub(tempDir, {
      device_token: "device-123",
      display_name: "Tafar",
      onboarded: true,
    }),
    fetch: async (url) => {
      if (String(url).includes("/api/v1/profile")) {
        return {
          ok: true,
          status: 200,
          async text() {
            return JSON.stringify({ display_name: "Tafar" });
          },
        };
      }
      throw new Error(`Unexpected URL: ${url}`);
    },
    platform: "win32",
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
  assert.equal(initializeCalls, 1);
  assert.equal(qrCalls[0], "qr-token");
  assert.equal(destroyCalls, 1);
  assert.ok(fakeConsole.messages.some(message => message.includes("Login complete")));
});

test("startWhatsAppGateway reuses the shared WhatsApp session directory", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, {
    device_token: "device-123",
    display_name: "Tafar",
    onboarded: true,
  });
  let receivedOptions = null;

  await startWhatsAppGateway({
    console: createConsole(),
    config,
    fetch: async (url) => {
      if (String(url).includes("/api/v1/profile")) {
        return {
          ok: true,
          status: 200,
          async text() {
            return JSON.stringify({ display_name: "Tafar" });
          },
        };
      }
      throw new Error(`Unexpected URL: ${url}`);
    },
    platform: "win32",
    qrTerminal: { generate() {} },
    whatsappModule: {
      LocalAuth: class LocalAuth {
        constructor(init) {
          Object.assign(this, init);
        }
      },
    },
    createClient: options => {
      receivedOptions = options;
      return {
        info: { wid: { _serialized: "me@c.us" } },
        on() {},
        async initialize() {},
      };
    },
  });

  assert.equal(receivedOptions.authStrategy.dataPath, getWhatsAppSessionDir(config));
});

test("startWhatsAppDaemon starts detached, writes pid/state files, and appends logs", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, null);
  const fakeConsole = createConsole();
  let unrefCalled = false;

  const exitCode = await startWhatsAppDaemon({
    console: fakeConsole,
    config,
    cwd: tempDir,
    env: { TEST_ENV: "1" },
    spawn(command, args, options) {
      assert.equal(command, process.execPath);
      assert.deepEqual(args.slice(-2), ["whatsapp", "--daemon-child"]);
      assert.equal(options.detached, true);
      assert.equal(options.windowsHide, true);
      assert.equal(options.env.PROPHET_CONFIG_DIR, tempDir);
      return {
        pid: 43210,
        unref() {
          unrefCalled = true;
        },
      };
    },
  });

  assert.equal(exitCode, 0);
  assert.equal(unrefCalled, true);
  assert.equal(fs.readFileSync(getWhatsAppDaemonPidPath(config), "utf8").trim(), "43210");
  assert.equal(fs.existsSync(getWhatsAppDaemonLogPath(config)), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(getWhatsAppDaemonStatePath(config), "utf8")), {
    pid: 43210,
    started_at: JSON.parse(fs.readFileSync(getWhatsAppDaemonStatePath(config), "utf8")).started_at,
    message_count: 0,
    status: "starting",
    log_path: getWhatsAppDaemonLogPath(config),
    pid_path: getWhatsAppDaemonPidPath(config),
    state_path: getWhatsAppDaemonStatePath(config),
  });
  assert.ok(fakeConsole.messages.some(message => message.includes("daemon started")));
});

test("startWhatsAppDaemon prevents duplicate starts when pid is active", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, null);
  fs.writeFileSync(getWhatsAppDaemonPidPath(config), `${process.pid}\n`, "utf8");
  const fakeConsole = createConsole();
  let spawnCalls = 0;

  const exitCode = await startWhatsAppDaemon({
    console: fakeConsole,
    config,
    spawn() {
      spawnCalls += 1;
      throw new Error("spawn should not run");
    },
  });

  assert.equal(exitCode, 0);
  assert.equal(spawnCalls, 0);
  assert.ok(fakeConsole.messages.some(message => message.includes("already running")));
});

test("printWhatsAppDaemonStatus cleans stale pid files", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, null);
  fs.writeFileSync(getWhatsAppDaemonPidPath(config), "999999\n", "utf8");
  fs.writeFileSync(getWhatsAppDaemonStatePath(config), JSON.stringify({ message_count: 7 }), "utf8");
  const fakeConsole = createConsole();

  const exitCode = printWhatsAppDaemonStatus({
    console: fakeConsole,
    config,
  });

  assert.equal(exitCode, 0);
  assert.equal(fs.existsSync(getWhatsAppDaemonPidPath(config)), false);
  assert.equal(fs.existsSync(getWhatsAppDaemonStatePath(config)), false);
  assert.ok(fakeConsole.messages.some(message => message.includes("not running")));
});

test("printWhatsAppDaemonStatus reports uptime and handled messages", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, null);
  fs.writeFileSync(getWhatsAppDaemonPidPath(config), `${process.pid}\n`, "utf8");
  fs.writeFileSync(getWhatsAppDaemonStatePath(config), JSON.stringify({
    started_at: Date.now() - (2 * 60 * 60 * 1000) - (14 * 60 * 1000),
    message_count: 47,
  }), "utf8");
  const fakeConsole = createConsole();

  const exitCode = printWhatsAppDaemonStatus({
    console: fakeConsole,
    config,
  });

  assert.equal(exitCode, 0);
  assert.ok(fakeConsole.messages.some(message => message.includes(`PID: ${process.pid}`)));
  assert.ok(fakeConsole.messages.some(message => message.includes("Uptime: 2 hours 14 minutes")));
  assert.ok(fakeConsole.messages.some(message => message.includes("Messages handled: 47")));
});

test("stopWhatsAppDaemon gracefully stops the daemon and clears pid/state files", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, null);
  const daemonPid = process.pid;
  const killSignals = [];

  fs.writeFileSync(getWhatsAppDaemonPidPath(config), `${daemonPid}\n`, "utf8");
  fs.writeFileSync(getWhatsAppDaemonStatePath(config), JSON.stringify({ started_at: Date.now(), message_count: 3 }), "utf8");
  const fakeConsole = createConsole();

  const exitCode = await stopWhatsAppDaemon({
    console: fakeConsole,
    config,
    processKill(pid, signal) {
      killSignals.push([pid, signal]);
    },
    stopTimeoutMs: 5_000,
    waitForProcessExit: async () => true,
  });

  assert.equal(exitCode, 0);
  assert.deepEqual(killSignals, [[daemonPid, "SIGTERM"]]);
  assert.equal(fs.existsSync(getWhatsAppDaemonPidPath(config)), false);
  assert.equal(fs.existsSync(getWhatsAppDaemonStatePath(config)), false);
  assert.ok(fakeConsole.messages.some(message => message.includes("daemon stopped")));
});

test("stopWhatsAppDaemon treats ESRCH during shutdown as an already-stopped daemon", async t => {
  const tempDir = withTempDir(t);
  const config = createConfigStub(tempDir, null);
  const daemonPid = process.pid;
  fs.writeFileSync(getWhatsAppDaemonPidPath(config), `${daemonPid}\n`, "utf8");
  fs.writeFileSync(getWhatsAppDaemonStatePath(config), JSON.stringify({ started_at: Date.now(), message_count: 1 }), "utf8");
  const fakeConsole = createConsole();

  const exitCode = await stopWhatsAppDaemon({
    console: fakeConsole,
    config,
    processKill() {
      const error = new Error("no such process");
      error.code = "ESRCH";
      throw error;
    },
    waitForProcessExit: async () => {
      throw new Error("waitForProcessExit should not run after ESRCH");
    },
  });

  assert.equal(exitCode, 0);
  assert.equal(fs.existsSync(getWhatsAppDaemonPidPath(config)), false);
  assert.equal(fs.existsSync(getWhatsAppDaemonStatePath(config)), false);
  assert.ok(fakeConsole.messages.some(message => message.includes("daemon stopped")));
});
