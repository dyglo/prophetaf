"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const childProcess = require("node:child_process");
const { ensureGatewayProfile, GatewayError } = require("./backend");
const { createInboundMessageHandler } = require("./inbound");
const { SentMessageTracker } = require("./outbound");
const {
  clearWhatsAppDaemonFiles,
  getWhatsAppDaemonLogPath,
  getWhatsAppDaemonPidPath,
  getWhatsAppDaemonStatePath,
  getWhatsAppSessionDir,
  readWhatsAppDaemonPid,
  readWhatsAppDaemonState,
  updateWhatsAppDaemonState,
  writeWhatsAppDaemonPid,
  writeWhatsAppDaemonState,
} = require("./state");

const REQUIRED_CHROMIUM_LIBRARIES = [
  "libnspr4",
  "libnss3",
  "libatk1.0-0",
  "libatk-bridge2.0-0",
  "libcups2",
  "libdrm2",
  "libxkbcommon0",
  "libxcomposite1",
  "libxdamage1",
  "libxfixes3",
  "libxrandr2",
  "libgbm1",
  "libasound2",
];
const LINUX_CHROMIUM_INSTALL_COMMAND = `sudo apt-get install -y ${REQUIRED_CHROMIUM_LIBRARIES.join(" ")}`;
const DEFAULT_STOP_TIMEOUT_MS = 10_000;

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

function parseMissingLibraries(output) {
  return String(output || "")
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line.includes("=> not found"))
    .map(line => line.split(/\s+/)[0])
    .filter(Boolean);
}

function resolveChromiumExecutablePath(options = {}) {
  if (typeof options.getChromiumExecutablePath === "function") {
    return options.getChromiumExecutablePath();
  }
  const candidates = [
    () => require("puppeteer"),
    () => require("puppeteer-core"),
  ];

  for (const loadCandidate of candidates) {
    try {
      const moduleValue = loadCandidate();
      if (moduleValue && typeof moduleValue.executablePath === "function") {
        const executablePath = moduleValue.executablePath();
        if (typeof executablePath === "string" && executablePath.trim()) {
          return executablePath;
        }
      }
    } catch {
      // Keep searching for the bundled Puppeteer dependency.
    }
  }

  return "";
}

function checkLinuxChromiumDependencies(options = {}) {
  const platform = options.platform || process.platform;
  if (platform !== "linux") {
    return { ok: true, skipped: true };
  }

  const executablePath = resolveChromiumExecutablePath(options);
  if (!executablePath) {
    return { ok: true, skipped: true };
  }

  const execFileSync = options.execFileSync || childProcess.execFileSync;
  try {
    const output = execFileSync("ldd", [executablePath], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    const missingLibraries = parseMissingLibraries(output);
    if (missingLibraries.length === 0) {
      return { ok: true, executablePath, missingLibraries: [] };
    }
    return {
      ok: false,
      executablePath,
      missingLibraries,
      installCommand: LINUX_CHROMIUM_INSTALL_COMMAND,
    };
  } catch (error) {
    const output = `${error && error.stdout ? error.stdout : ""}\n${error && error.stderr ? error.stderr : ""}`;
    const missingLibraries = parseMissingLibraries(output);
    if (missingLibraries.length === 0) {
      return { ok: true, executablePath, skipped: true };
    }
    return {
      ok: false,
      executablePath,
      missingLibraries,
      installCommand: LINUX_CHROMIUM_INSTALL_COMMAND,
    };
  }
}

function formatLinuxDependencyError(details) {
  const missingText = Array.isArray(details && details.missingLibraries) && details.missingLibraries.length > 0
    ? ` Missing libraries: ${details.missingLibraries.join(", ")}.`
    : "";
  return [
    "Chromium is missing required Linux shared libraries before WhatsApp could start.",
    missingText.trim(),
    `On Ubuntu/Debian run: ${LINUX_CHROMIUM_INSTALL_COMMAND}`,
  ]
    .filter(Boolean)
    .join(" ");
}

function getDaemonMode(options = {}) {
  return options.mode || "foreground";
}

function isDaemonChild(options = {}) {
  return getDaemonMode(options) === "daemon-child";
}

function isProcessRunning(pid) {
  if (!Number.isInteger(pid) || pid <= 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function cleanupStaleDaemonState(config) {
  const pid = readWhatsAppDaemonPid(config);
  if (!pid) {
    clearWhatsAppDaemonFiles(config);
    return null;
  }
  if (isProcessRunning(pid)) {
    return pid;
  }
  clearWhatsAppDaemonFiles(config);
  return null;
}

function formatUptime(startedAt, now = Date.now()) {
  if (!Number.isFinite(startedAt) || startedAt <= 0) {
    return "unknown";
  }
  const totalMinutes = Math.max(0, Math.floor((now - startedAt) / 60_000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours <= 0) {
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  return `${hours} hour${hours === 1 ? "" : "s"} ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

function daemonStateSnapshot(config) {
  const state = readWhatsAppDaemonState(config);
  const pid = cleanupStaleDaemonState(config);
  if (!pid) {
    return { running: false, pid: null, state };
  }
  return {
    running: true,
    pid,
    state,
  };
}

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForProcessExit(pid, options = {}) {
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : DEFAULT_STOP_TIMEOUT_MS;
  const intervalMs = Number.isFinite(options.intervalMs) ? options.intervalMs : 100;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isProcessRunning(pid)) {
      return true;
    }
    await wait(intervalMs);
  }
  return !isProcessRunning(pid);
}

function resolveCliEntrypoint() {
  return path.resolve(__dirname, "../../bin/prophetaf.js");
}

function resolveConfigDir(config) {
  return config && typeof config.getConfigDir === "function"
    ? config.getConfigDir()
    : null;
}

async function startWhatsAppDaemon(options = {}) {
  const consoleLike = options.console || global.console;
  const config = options.config;
  const existingPid = cleanupStaleDaemonState(config);
  if (existingPid) {
    consoleLike.log(`Prophet WhatsApp daemon is already running. PID: ${existingPid}`);
    return 0;
  }

  const logPath = getWhatsAppDaemonLogPath(config);
  const configDir = resolveConfigDir(config);
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  const outFd = fs.openSync(logPath, "a");
  const errFd = fs.openSync(logPath, "a");
  try {
    const child = (options.spawn || childProcess.spawn)(
      process.execPath,
      [resolveCliEntrypoint(), "whatsapp", "--daemon-child"],
      {
        cwd: options.cwd || process.cwd(),
        detached: true,
        stdio: ["ignore", outFd, errFd],
        env: {
          ...process.env,
          ...(options.env || {}),
          ...(configDir ? { PROPHET_CONFIG_DIR: configDir } : {}),
          PROPHET_DAEMON_CHILD: "1",
        },
        windowsHide: true,
      },
    );
    child.unref();
    writeWhatsAppDaemonPid(child.pid, config);
    writeWhatsAppDaemonState({
      pid: child.pid,
      started_at: Date.now(),
      message_count: 0,
      status: "starting",
      log_path: logPath,
      pid_path: getWhatsAppDaemonPidPath(config),
      state_path: getWhatsAppDaemonStatePath(config),
    }, config);
    consoleLike.log(`Prophet WhatsApp daemon started. PID: ${child.pid}`);
    consoleLike.log("Prophet is now online on WhatsApp.");
    consoleLike.log('Run "prophetaf whatsapp --stop" to take Prophet offline.');
    return 0;
  } finally {
    fs.closeSync(outFd);
    fs.closeSync(errFd);
  }
}

async function stopWhatsAppDaemon(options = {}) {
  const consoleLike = options.console || global.console;
  const config = options.config;
  const pid = cleanupStaleDaemonState(config);
  if (!pid) {
    consoleLike.log("Prophet WhatsApp daemon is not running.");
    consoleLike.log('Run "prophetaf whatsapp --daemon" to start.');
    return 0;
  }

  process.kill(pid, "SIGTERM");
  let exited = await waitForProcessExit(pid, { timeoutMs: options.stopTimeoutMs });
  if (!exited) {
    process.kill(pid, "SIGKILL");
    exited = await waitForProcessExit(pid, { timeoutMs: 2_000 });
  }
  clearWhatsAppDaemonFiles(config);
  if (!exited) {
    throw new GatewayError("WhatsApp daemon could not be stopped cleanly.");
  }
  consoleLike.log("Prophet WhatsApp daemon stopped. Prophet is now offline.");
  return 0;
}

function printWhatsAppDaemonStatus(options = {}) {
  const consoleLike = options.console || global.console;
  const config = options.config;
  const snapshot = daemonStateSnapshot(config);
  if (!snapshot.running) {
    consoleLike.log("Prophet WhatsApp daemon is not running.");
    if (snapshot.state && typeof snapshot.state.last_error === "string" && snapshot.state.last_error.trim()) {
      consoleLike.log(`Last startup error: ${snapshot.state.last_error}`);
    }
    consoleLike.log('Run "prophetaf whatsapp --daemon" to start.');
    return 0;
  }

  const messageCount = Number.isFinite(snapshot.state.message_count) ? snapshot.state.message_count : 0;
  consoleLike.log(`Prophet WhatsApp daemon is running. PID: ${snapshot.pid}`);
  consoleLike.log(`Uptime: ${formatUptime(Number(snapshot.state.started_at || 0))}`);
  consoleLike.log(`Messages handled: ${messageCount}`);
  return 0;
}

function markDaemonReady(config) {
  updateWhatsAppDaemonState({
    status: "running",
    ready_at: Date.now(),
  }, config);
}

function incrementHandledMessageCount(config) {
  const current = readWhatsAppDaemonState(config);
  const currentCount = Number.isFinite(current.message_count) ? current.message_count : 0;
  updateWhatsAppDaemonState({
    message_count: currentCount + 1,
    last_message_at: Date.now(),
  }, config);
}

function registerShutdownHandlers(client, options = {}) {
  if (!isDaemonChild(options)) {
    return () => {};
  }

  const consoleLike = options.console || global.console;
  const config = options.config;
  const keepAliveTimer = options.keepAliveTimer || null;
  let shuttingDown = false;
  const shutdown = async reason => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    try {
      updateWhatsAppDaemonState({
        status: "stopping",
        stopped_at: Date.now(),
        stop_reason: reason || "shutdown",
      }, config);
      if (client && typeof client.destroy === "function") {
        await client.destroy();
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      consoleLike.log(`WhatsApp daemon shutdown failed: ${detail}`);
    } finally {
      if (keepAliveTimer) {
        clearInterval(keepAliveTimer);
      }
      const pid = readWhatsAppDaemonPid(config);
      if (pid === process.pid) {
        clearWhatsAppDaemonFiles(config);
      }
      process.exit(0);
    }
  };

  const onSigterm = () => {
    shutdown("SIGTERM");
  };
  const onSigint = () => {
    shutdown("SIGINT");
  };
  process.once("SIGTERM", onSigterm);
  process.once("SIGINT", onSigint);
  return () => {
    process.removeListener("SIGTERM", onSigterm);
    process.removeListener("SIGINT", onSigint);
  };
}

async function runForegroundGateway(options = {}) {
  const fetchImpl = options.fetch || global.fetch;
  const consoleLike = options.console || global.console;
  const config = options.config;
  const whatsappModule = options.whatsappModule || require("whatsapp-web.js");
  const qrTerminal = options.qrTerminal || require("qrcode-terminal");
  const loginOnly = options.loginOnly === true;

  if (typeof fetchImpl !== "function") {
    throw new GatewayError("This runtime does not provide fetch. Use Node.js 18 or newer.");
  }

  if (isDaemonChild(options)) {
    updateWhatsAppDaemonState({
      status: "booting",
      boot_started_at: Date.now(),
      last_error: null,
      last_error_at: null,
    }, config);
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
  const keepAliveTimer = isDaemonChild(options)
    ? setInterval(() => {}, 60_000)
    : null;
  try {
    const dependencyCheck = checkLinuxChromiumDependencies({
      platform: options.platform || os.platform(),
      execFileSync: options.execFileSync,
      getChromiumExecutablePath: options.getChromiumExecutablePath,
    });
    if (!dependencyCheck.ok) {
      throw new GatewayError(formatLinuxDependencyError(dependencyCheck));
    }

    let ownChatId = "";
    registerShutdownHandlers(client, {
      ...options,
      keepAliveTimer,
    });
    const handleInboundMessage = createInboundMessageHandler({
      ...options,
      fetch: fetchImpl,
      console: consoleLike,
      config,
      tracker,
      gatewayStartedAt,
      getOwnChatId: () => ownChatId,
      onHandled: isDaemonChild(options)
        ? () => incrementHandledMessageCount(config)
        : null,
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
      if (isDaemonChild(options)) {
        markDaemonReady(config);
      }
      if (loginOnly) {
        consoleLike.log("Login complete. You can now run prophetaf whatsapp.");
        if (typeof client.destroy === "function") {
          await client.destroy();
        }
      }
    });

    client.on("disconnected", reason => {
      if (isDaemonChild(options)) {
        updateWhatsAppDaemonState({
          status: "disconnected",
          disconnected_at: Date.now(),
          disconnect_reason: reason || "unknown reason",
        }, config);
      }
      consoleLike.log(`WhatsApp gateway disconnected: ${reason || "unknown reason"}`);
    });

    client.on("message_create", async message => {
      try {
        await handleInboundMessage(message);
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        if (isDaemonChild(options)) {
          updateWhatsAppDaemonState({
            last_error: detail,
            last_error_at: Date.now(),
          }, config);
        }
        consoleLike.log(`WhatsApp message handling failed: ${detail}`);
      }
    });

    await client.initialize();
    return 0;
  } catch (error) {
    if (keepAliveTimer) {
      clearInterval(keepAliveTimer);
    }
    throw error;
  }
}

async function startWhatsAppGateway(options = {}) {
  const mode = getDaemonMode(options);
  if (mode === "daemon") {
    return startWhatsAppDaemon(options);
  }
  if (mode === "stop") {
    return stopWhatsAppDaemon(options);
  }
  if (mode === "status") {
    return printWhatsAppDaemonStatus(options);
  }
  try {
    return await runForegroundGateway(options);
  } catch (error) {
    if (isDaemonChild(options)) {
      updateWhatsAppDaemonState({
        status: "failed",
        failed_at: Date.now(),
        last_error: error instanceof Error ? error.message : String(error),
        last_error_at: Date.now(),
      }, options.config);
    }
    throw error;
  }
}

module.exports = {
  checkLinuxChromiumDependencies,
  cleanupStaleDaemonState,
  createClientOptions,
  daemonStateSnapshot,
  formatLinuxDependencyError,
  formatUptime,
  isProcessRunning,
  LINUX_CHROMIUM_INSTALL_COMMAND,
  printWhatsAppDaemonStatus,
  REQUIRED_CHROMIUM_LIBRARIES,
  startWhatsAppDaemon,
  startWhatsAppGateway,
  stopWhatsAppDaemon,
  waitForProcessExit,
};
