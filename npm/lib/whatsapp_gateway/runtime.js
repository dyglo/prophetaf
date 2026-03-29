"use strict";

const fs = require("node:fs");
const os = require("node:os");
const childProcess = require("node:child_process");
const { ensureGatewayProfile, GatewayError } = require("./backend");
const { createInboundMessageHandler } = require("./inbound");
const { SentMessageTracker } = require("./outbound");
const { getWhatsAppSessionDir } = require("./state");

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
  const dependencyCheck = checkLinuxChromiumDependencies({
    platform: options.platform || os.platform(),
    execFileSync: options.execFileSync,
    getChromiumExecutablePath: options.getChromiumExecutablePath,
  });
  if (!dependencyCheck.ok) {
    throw new GatewayError(formatLinuxDependencyError(dependencyCheck));
  }

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
  checkLinuxChromiumDependencies,
  createClientOptions,
  formatLinuxDependencyError,
  LINUX_CHROMIUM_INSTALL_COMMAND,
  REQUIRED_CHROMIUM_LIBRARIES,
  startWhatsAppGateway,
};
