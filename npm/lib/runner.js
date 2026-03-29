"use strict";

const { name: PACKAGE_NAME, version: CLI_VERSION } = require("../package.json");
const readline = require("node:readline/promises");
const configStore = require("./config");
const {
  detectImagePaths,
  readImageAsBase64,
  stripImagePathsFromMessage,
  validateImageFile,
} = require("./image_handler");
const { runOnboarding } = require("./onboarding");
let updateNotifierModulePromise;

const PROPHET_BANNER = `
  ██████╗ ██████╗  ██████╗ ██████╗ ██╗  ██╗███████╗████████╗
  ██╔══██╗██╔══██╗██╔═══██╗██╔══██╗██║  ██║██╔════╝╚══██╔══╝
  ██████╔╝██████╔╝██║   ██║██████╔╝███████║█████╗     ██║
  ██╔═══╝ ██╔══██╗██║   ██║██╔═══╝ ██╔══██║██╔══╝     ██║
  ██║     ██║  ██║╚██████╔╝██║     ██║  ██║███████╗   ██║
  ╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝  ╚═╝╚══════╝   ╚═╝
`;
const PROPHET_VERSION_LINE = `Personal AI Trading Assistant | v${CLI_VERSION} | Cloud Edition`;

const BACKEND_BASE_URL = "https://prophet-wwxjsbvhoa-uc.a.run.app";
const SPINNER_FRAMES = ["◐", "◓", "◑", "◒"];
const UPDATE_CHECK_INTERVAL_MS = 1000 * 60 * 60;
const UPDATE_NOTIFICATION_DELAY_MS = 2000;
const ANSI_RESET = "\u001b[0m";
const ANSI_BOLD = "\u001b[1m";
const ANSI_DIM = "\u001b[2m";
const ANSI_CYAN = "\u001b[36m";
const ANSI_BLUE = "\u001b[34m";
const ANSI_GREEN = "\u001b[32m";
const ANSI_YELLOW = "\u001b[33m";
const ANSI_MAGENTA = "\u001b[35m";
const ANSI_WHITE = "\u001b[37m";
const ANSI_GRAY = "\u001b[90m";
const ANSI_STRIP_PATTERN = /\u001b\[[0-9;]*m/g;
const EXPLICIT_WEB_HINT_PATTERN = /\b(headline|headlines|breaking|news|live news|latest news|search the web|web search|look up|look this up|search online)\b/i;
const EVENT_RISK_PATTERN = /\b(cpi|nfp|fomc|fed|ecb|boe|boj|tariff|geopolitical|rate decision|inflation print)\b/i;
const RECENCY_PATTERN = /\b(today|latest|current|live|now|recent)\b/i;

const LABEL_SETS = {
  scan: [
    "Sweeping the watchlist...",
    "Scanning market structure...",
    "Checking confluence zones...",
    "Reading recent candles...",
    "Sizing up momentum...",
    "Reviewing breakout pressure...",
    "Watching session overlap...",
    "Measuring structure quality...",
    "Ranking setup strength...",
    "Filtering noisy pairs...",
  ],
  bias: [
    "Reading directional flow...",
    "Mapping higher-timeframe bias...",
    "Checking session posture...",
    "Measuring trend pressure...",
    "Reviewing swing structure...",
    "Tracing liquidity path...",
    "Weighing continuation odds...",
    "Scanning directional clues...",
    "Balancing bullish and bearish cases...",
    "Locking in market bias...",
  ],
  risk: [
    "Calibrating position size...",
    "Sizing the trade...",
    "Checking risk exposure...",
    "Balancing stop and size...",
    "Calculating capital at risk...",
    "Projecting position units...",
    "Normalizing risk per pip...",
    "Matching stop distance...",
    "Stress-testing the setup...",
    "Finalizing trade size...",
  ],
  command: [
    "Checking command state...",
    "Refreshing command context...",
    "Opening the control panel...",
    "Syncing session controls...",
    "Loading command options...",
    "Preparing command output...",
    "Reviewing active session tools...",
    "Inspecting session settings...",
    "Resolving command view...",
    "Composing command response...",
  ],
  web: [
    "Searching the web...",
    "Scanning live headlines...",
    "Checking macro catalysts...",
    "Pulling fresh market context...",
    "Reviewing breaking developments...",
    "Reading latest news flow...",
    "Cross-checking live sources...",
    "Looking for event risk...",
    "Gathering current web signals...",
    "Searching for fresh context...",
  ],
  chat: [
    "Thinking through the setup...",
    "Propheting through the noise...",
    "Mapping the market picture...",
    "Linking structure and macro...",
    "Reviewing the current context...",
    "Reading the session pulse...",
    "Weighing the strongest angle...",
    "Distilling the cleanest answer...",
    "Connecting the trade narrative...",
    "Sharpening the response...",
  ],
};

const SPINNER_THEME = {
  scan: { frame: ANSI_CYAN, label: ANSI_WHITE },
  bias: { frame: ANSI_GREEN, label: ANSI_WHITE },
  risk: { frame: ANSI_YELLOW, label: ANSI_WHITE },
  command: { frame: ANSI_MAGENTA, label: ANSI_WHITE },
  web: { frame: ANSI_BLUE, label: ANSI_YELLOW },
  chat: { frame: ANSI_CYAN, label: ANSI_WHITE },
};

class UserError extends Error {
  constructor(message, exitCode = 1) {
    super(message);
    this.name = "UserError";
    this.exitCode = exitCode;
  }
}

function normalizeCommand(token) {
  if (!token) {
    return "chat";
  }

  if (["chat", "scan", "bias", "risk", "resume"].includes(token)) {
    return token;
  }

  return "chat";
}

function parseFlags(args) {
  const flags = {};
  const positionals = [];

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (!arg.startsWith("--")) {
      positionals.push(arg);
      continue;
    }

    const key = arg.slice(2);
    const next = args[index + 1];
    if (!next || next.startsWith("--")) {
      throw new UserError(`Missing value for --${key}`);
    }

    flags[key] = next;
    index += 1;
  }

  return { flags, positionals };
}

function parseCommand(argv) {
  const filtered = (argv || []).filter(arg => arg !== undefined && arg !== null);
  if (filtered.includes("--help") || filtered.includes("-h")) {
    return { command: "help" };
  }
  const first = filtered[0];
  const command = normalizeCommand(first);
  const rest = command === "chat" && first !== "chat" ? filtered : filtered.slice(1);
  const { flags, positionals } = parseFlags(rest);

  if (command === "scan" || command === "bias") {
    return {
      command,
      payload: flags.pair ? { pair: flags.pair } : {},
    };
  }

  if (command === "risk") {
    if (!flags.pair || !flags.sl || !flags.risk) {
      throw new UserError("risk requires --pair, --sl, and --risk");
    }

    const sl = Number(flags.sl);
    const risk = Number(flags.risk);
    if (!Number.isFinite(sl) || !Number.isInteger(sl) || sl <= 0) {
      throw new UserError("--sl must be a positive integer");
    }
    if (!Number.isFinite(risk) || risk <= 0) {
      throw new UserError("--risk must be a positive number");
    }

    return {
      command,
      payload: {
        pair: flags.pair,
        sl,
        risk,
      },
    };
  }

  if (command === "resume") {
    return {
      command,
    };
  }

  return {
    command: "chat",
    message: positionals.join(" ").trim(),
  };
}

function formatHelpText() {
  return [
    "Usage: prophetaf [command] [message]",
    "",
    "Commands:",
    "  chat [message]            Send a natural-language chat request",
    "  scan --pair PAIR          Run the scan endpoint for one pair",
    "  bias --pair PAIR          Run the bias endpoint for one pair",
    "  risk --pair PAIR --sl N --risk PCT",
    "                            Calculate position size",
    "  resume                    Resume the latest saved session",
    "",
    "Flags:",
    "  -h, --help                Show this help message",
  ].join("\n");
}

function responseHeaderValue(response, name) {
  if (!response || !response.headers) {
    return "";
  }
  if (typeof response.headers.get === "function") {
    return response.headers.get(name) || "";
  }
  const direct = response.headers[name] || response.headers[name.toLowerCase()];
  return typeof direct === "string" ? direct : "";
}

function buildDeviceHeaders(configModule = configStore) {
  const token = configModule && typeof configModule.getDeviceToken === "function"
    ? configModule.getDeviceToken()
    : null;
  return token ? { "X-Device-Token": token } : {};
}

async function requestJson(fetchImpl, path, payload, options = {}) {
  const method = options.method || "POST";
  const headers = { ...(options.headers || {}) };
  const request = {
    method,
    headers,
  };
  if (payload !== undefined && payload !== null) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
    request.body = JSON.stringify(payload);
  }

  const response = await fetchImpl(`${BACKEND_BASE_URL}${path}`, request);

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!response.ok) {
    const details = typeof data === "string" ? data : JSON.stringify(data);
    const error = new UserError(`Backend request failed (${response.status}): ${details}`);
    error.status = response.status;
    throw error;
  }

  return data;
}

async function requestGetJson(fetchImpl, path, query = null, options = {}) {
  const suffix = query ? `?${new URLSearchParams(query).toString()}` : "";
  const response = await fetchImpl(`${BACKEND_BASE_URL}${path}${suffix}`, {
    method: "GET",
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    const details = typeof data === "string" ? data : JSON.stringify(data);
    const error = new UserError(`Backend request failed (${response.status}): ${details}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function formatWelcomeBackMessage(displayName) {
  return [
    "──────────────────────────────────────────",
    `  Welcome back, ${displayName}.`,
    "  Your Prophet profile is loaded.",
    "  Type anything to begin your session.",
    "──────────────────────────────────────────",
  ].join("\n");
}

async function ensureProfile(fetchImpl, consoleLike, overrides = {}) {
  const config = overrides.config || configStore;
  const onboarding = overrides.runOnboarding || runOnboarding;
  const requestHeaders = () => buildDeviceHeaders(config);
  const isConfigValid = candidate =>
    typeof config.isConfigValid === "function"
      ? config.isConfigValid(candidate)
      : Boolean(
        candidate
        && typeof candidate === "object"
        && typeof candidate.device_token === "string"
        && candidate.device_token.trim().length > 0
        && candidate.onboarded === true,
      );

  const runOnboardingFlow = async () =>
    onboarding({
      console: consoleLike,
      fetch: fetchImpl,
      config,
      prompts: overrides.prompts,
      stdin: overrides.stdin,
      stdout: overrides.stdout,
      backendBaseUrl: BACKEND_BASE_URL,
    });

  // Diagnosis: npm global updates do not delete ~/.prophet/config.json because config.js
  // resolves the file under os.homedir(). The onboarding trigger lives entirely in this
  // startup gate: missing config, unreadable/incomplete config, or a stored token that the
  // backend no longer recognizes. Centralizing validation here prevents valid saved profiles
  // from being treated like first-run users after package updates.
  if (!config.configExists || !config.configExists()) {
    return runOnboardingFlow();
  }

  const existing = config.readConfig && config.readConfig();
  if (!isConfigValid(existing)) {
    consoleLike.log("Warning: Profile config appears incomplete. Starting setup.");
    if (typeof config.clearConfig === "function") {
      config.clearConfig();
    }
    return runOnboardingFlow();
  }

  try {
    const profile = await requestGetJson(fetchImpl, "/api/v1/profile", null, {
      headers: requestHeaders(),
    });
    consoleLike.log(formatWelcomeBackMessage(profile.display_name));
    return { status: "loaded", profile };
  } catch (error) {
    if (error instanceof UserError && (error.status === 404 || error.status === 400)) {
      if (typeof config.clearConfig === "function") {
        config.clearConfig();
      }
      return runOnboardingFlow();
    }
    consoleLike.log("Warning: Prophet could not verify your saved profile. Continuing without profile sync.");
    return { status: "offline" };
  }
}

function printJson(consoleLike, data) {
  consoleLike.log(JSON.stringify(data, null, 2));
}

function writeLine(stream, value) {
  if (stream && typeof stream.write === "function") {
    stream.write(value);
  }
}

function clearCurrentLine(stream, width = 80) {
  writeLine(stream, `\r${" ".repeat(width)}\r`);
}

function formatUpdateNotification(currentVersion, latestVersion) {
  return [
    "──────────────────────────────────────────────────",
    `  New version available: v${latestVersion}`,
    `  You are running:       v${currentVersion}`,
    "",
    "  Run the following to update:",
    "  npm install -g prophetaf@latest",
    "──────────────────────────────────────────────────",
  ].join("\n");
}

function pause(ms) {
  return new Promise(resolve => {
    setTimeout(resolve, ms);
  });
}

function getUpdateNotifierModulePromise() {
  if (!updateNotifierModulePromise) {
    updateNotifierModulePromise = import("update-notifier").catch(() => null);
  }
  return updateNotifierModulePromise;
}

async function startUpdateCheck(options = {}) {
  try {
    const currentVersion = options.currentVersion || CLI_VERSION;
    const packageName = options.packageName || PACKAGE_NAME;
    const loadUpdateNotifier = options.loadUpdateNotifier
      || (async () => {
        const moduleValue = await getUpdateNotifierModulePromise();
        return moduleValue && (moduleValue.default || moduleValue);
      });
    const updateNotifier = await loadUpdateNotifier();
    if (typeof updateNotifier !== "function") {
      return null;
    }

    // update-notifier only exposes the last cached registry result on this launch.
    // It refreshes that cache in the background for a later launch.
    const notifier = updateNotifier({
      pkg: {
        name: packageName,
        version: currentVersion,
      },
      updateCheckInterval: options.updateCheckInterval ?? UPDATE_CHECK_INTERVAL_MS,
    });
    const update = notifier && notifier.update;
    if (
      !update
      || typeof update.current !== "string"
      || typeof update.latest !== "string"
      || update.current.trim().length === 0
      || update.latest.trim().length === 0
    ) {
      return null;
    }

    return {
      currentVersion: update.current,
      latestVersion: update.latest,
    };
  } catch {
    return null;
  }
}

function supportsStyle(stream) {
  return Boolean(stream && stream.isTTY);
}

function shouldPauseForUpdateNotice(parsedCommand) {
  return Boolean(
    parsedCommand
    && (
      parsedCommand.command === "resume"
      || (parsedCommand.command === "chat" && !parsedCommand.message)
    ),
  );
}

function stylize(text, color, enabled, options = {}) {
  if (!enabled) {
    return text;
  }

  const open = `${options.dim ? ANSI_DIM : ""}${options.bold ? ANSI_BOLD : ""}${color || ""}`;
  return `${open}${text}${ANSI_RESET}`;
}

function visibleLength(text) {
  return String(text || "").replace(ANSI_STRIP_PATTERN, "").length;
}

function getWrapWidth(stream) {
  const columns = stream && Number.isFinite(stream.columns) && stream.columns > 0
    ? Math.floor(stream.columns)
    : 100;
  return Math.max(20, columns);
}

function wrapLine(line, width, firstIndent = "", restIndent = "") {
  const text = String(line || "");
  const firstIndentWidth = visibleLength(firstIndent);
  const restIndentWidth = visibleLength(restIndent);
  const lines = [];
  let currentIndent = firstIndent;
  let currentIndentWidth = firstIndentWidth;
  let current = currentIndent;
  let currentWidth = currentIndentWidth;

  for (const word of text.split(/\s+/).filter(Boolean)) {
    const wordWidth = visibleLength(word);
    const separator = currentWidth > currentIndentWidth ? " " : "";
    const separatorWidth = separator ? 1 : 0;
    const nextWidth = currentWidth + separatorWidth + wordWidth;

    if (nextWidth <= width) {
      current += `${separator}${word}`;
      currentWidth = nextWidth;
      continue;
    }

    if (currentWidth > currentIndentWidth) {
      lines.push(current);
      currentIndent = restIndent;
      currentIndentWidth = restIndentWidth;
      current = currentIndent;
      currentWidth = currentIndentWidth;
    }

    let remaining = word;
    while (visibleLength(remaining) > Math.max(1, width - currentIndentWidth)) {
      const sliceLength = Math.max(1, width - currentIndentWidth);
      const chunk = remaining.slice(0, sliceLength);
      lines.push(`${currentIndent}${chunk}`);
      remaining = remaining.slice(sliceLength);
      currentIndent = restIndent;
      currentIndentWidth = restIndentWidth;
      current = currentIndent;
      currentWidth = currentIndentWidth;
    }

    const postSeparator = currentWidth > currentIndentWidth ? " " : "";
    current += `${postSeparator}${remaining}`;
    currentWidth += (postSeparator ? 1 : 0) + visibleLength(remaining);
  }

  if (currentWidth > currentIndentWidth || text.length === 0) {
    lines.push(current);
  }

  return lines;
}

function wrapText(text, width, options = {}) {
  const firstIndent = options.firstIndent || "";
  const restIndent = options.restIndent || "";
  const lines = String(text || "").split("\n");
  return lines
    .flatMap((line, index) => {
      if (!line.trim()) {
        return [index === 0 ? firstIndent.trimEnd() : restIndent.trimEnd()];
      }
      return wrapLine(line, width, index === 0 ? firstIndent : restIndent, restIndent);
    })
    .join("\n");
}

function appendWrappedChunk(stream, state, chunk) {
  if (!state) {
    return;
  }

  const writeIndent = () => {
    if (state.lineLength === 0 && state.subsequentIndent) {
      writeLine(stream, state.subsequentIndent);
      state.lineLength = state.subsequentIndent.length;
    }
  };

  const pushNewLine = () => {
    writeLine(stream, "\n");
    state.lineLength = 0;
    state.pendingSpace = false;
    writeIndent();
  };

  const flushWord = () => {
    if (!state.pendingWord) {
      return;
    }

    writeIndent();
    let word = state.pendingWord;
    const separator = state.pendingSpace && state.lineLength > state.baseIndentLength ? " " : "";
    const fitsCurrentLine = state.lineLength + separator.length + visibleLength(word) <= state.width;

    if (fitsCurrentLine) {
      writeLine(stream, `${separator}${word}`);
      state.lineLength += separator.length + visibleLength(word);
      state.pendingWord = "";
      state.pendingSpace = false;
      return;
    }

    if (separator && state.lineLength > state.baseIndentLength) {
      pushNewLine();
      writeIndent();
    }

    while (visibleLength(word) > Math.max(1, state.width - state.lineLength)) {
      const chunkLength = Math.max(1, state.width - state.lineLength);
      writeLine(stream, word.slice(0, chunkLength));
      word = word.slice(chunkLength);
      if (word) {
        pushNewLine();
      }
    }

    if (word) {
      writeLine(stream, word);
      state.lineLength += visibleLength(word);
    }

    state.pendingWord = "";
    state.pendingSpace = false;
  };

  for (const character of String(chunk)) {
    if (character === "\r") {
      continue;
    }
    if (character === "\n") {
      flushWord();
      pushNewLine();
      continue;
    }
    if (/\s/.test(character)) {
      flushWord();
      state.pendingSpace = true;
      continue;
    }
    state.pendingWord += character;
  }
}

function flushWrappedChunk(stream, state) {
  if (!state || !state.pendingWord) {
    return;
  }
  appendWrappedChunk(stream, state, " ");
}

function shuffleLabels(labels, randomFn = Math.random) {
  const copy = [...labels];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const nextIndex = Math.floor(randomFn() * (index + 1));
    [copy[index], copy[nextIndex]] = [copy[nextIndex], copy[index]];
  }
  return copy;
}

function shouldUseWebSpinner(message) {
  const value = String(message || "").trim();
  if (!value) {
    return false;
  }
  if (EXPLICIT_WEB_HINT_PATTERN.test(value)) {
    return true;
  }
  return EVENT_RISK_PATTERN.test(value) && RECENCY_PATTERN.test(value);
}

function detectSpinnerMode(path, payload) {
  if (path === "/scan") {
    return "scan";
  }
  if (path === "/bias") {
    return "bias";
  }
  if (path === "/risk") {
    return "risk";
  }

  const message = String(payload && payload.message ? payload.message : "").trim();
  if (message.startsWith("/")) {
    return "command";
  }
  if (shouldUseWebSpinner(message)) {
    return "web";
  }
  return "chat";
}

function loadingLabelsFor(path, payload, options = {}) {
  const mode = detectSpinnerMode(path, payload);
  return shuffleLabels(LABEL_SETS[mode], options.randomFn);
}

function formatSpinnerText(frame, label, theme, enabled) {
  const frameText = stylize(frame, theme.frame, enabled, { bold: true });
  const labelText = stylize(label, theme.label, enabled, { bold: true });
  const trail = stylize("  •  Prophet is working", ANSI_GRAY, enabled, { dim: true });
  return `${frameText} ${labelText}${trail}`;
}

function createSpinner(stream, labels, options = {}) {
  if (!stream || !stream.isTTY || typeof stream.write !== "function") {
    return {
      start() {},
      stop() {},
      setLabel() {},
    };
  }

  const theme = SPINNER_THEME[options.mode] || SPINNER_THEME.chat;
  const styled = supportsStyle(stream);
  let frameIndex = 0;
  let labelIndex = 0;
  let intervalId = null;
  let lastWidth = 0;
  let overrideLabel = null;

  const draw = () => {
    const label = overrideLabel || labels[labelIndex % labels.length];
    const frame = SPINNER_FRAMES[frameIndex % SPINNER_FRAMES.length];
    const text = formatSpinnerText(frame, label, theme, styled);
    lastWidth = Math.max(lastWidth, visibleLength(text));
    writeLine(stream, `\r${text}`);
    frameIndex += 1;
    if (!overrideLabel && frameIndex % SPINNER_FRAMES.length === 0) {
      labelIndex += 1;
    }
  };

  return {
    start() {
      if (intervalId !== null) {
        return;
      }
      draw();
      intervalId = setInterval(draw, 100);
    },
    stop() {
      if (intervalId !== null) {
        clearInterval(intervalId);
        intervalId = null;
      }
      clearCurrentLine(stream, lastWidth);
    },
    setLabel(label) {
      overrideLabel = label ? String(label) : null;
      if (intervalId !== null) {
        draw();
      }
    },
  };
}

function supportsInteractive(input, output) {
  return Boolean(input && input.isTTY && output && output.isTTY);
}

function formatCalendarPayload(payload) {
  const events = Array.isArray(payload && payload.events) ? payload.events : [];
  const warnings = Array.isArray(payload && payload.warnings) ? payload.warnings : [];
  if (events.length === 0 && warnings.length === 0) {
    return "No calendar events returned for that view.";
  }

  const lines = events.map(event =>
    `${event.date} ${event.time_utc} UTC | ${event.currency} | ${event.impact} | ${event.event_name}`,
  );
  for (const warning of warnings) {
    lines.push(`Warning: ${warning.message}`);
  }
  return lines.join("\n");
}

async function loadPrompts(overrides) {
  if (overrides.prompts) {
    return overrides.prompts;
  }
  return import("@inquirer/prompts");
}

function isPromptCancelError(error) {
  if (!error || typeof error !== "object") {
    return false;
  }
  const name = typeof error.name === "string" ? error.name : "";
  const message = typeof error.message === "string" ? error.message : "";
  return (
    name === "ExitPromptError"
    || name === "AbortPromptError"
    || message.includes("User force closed")
    || message.includes("Prompt was canceled")
  );
}

function parseSseEvents(buffer, onEvent) {
  let remaining = buffer;
  while (true) {
    const boundary = remaining.indexOf("\n\n");
    if (boundary === -1) {
      break;
    }
    const rawEvent = remaining.slice(0, boundary);
    remaining = remaining.slice(boundary + 2);
    const lines = rawEvent.split(/\r?\n/);
    let eventName = "message";
    const dataLines = [];
    for (const line of lines) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length === 0) {
      continue;
    }
    let payload = dataLines.join("\n");
    try {
      payload = JSON.parse(payload);
    } catch {
      // Keep raw payload when it is not JSON.
    }
    onEvent(eventName, payload);
  }
  return remaining;
}

function stripResidualMarkdownMarkers(text) {
  return String(text || "")
    .replace(/\*\*/g, "")
    .replace(/__/g, "");
}

function stripMarkdownSyntax(text) {
  return String(text || "")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map(line => line
      .replace(/^(\s*)#{1,6}\s*/g, "$1")
      .replace(/^(\s*)[-*]\s+/g, "$1• ")
      .replace(/\[(.+?)\]\((.+?)\)/g, "$1")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/\*\*(.+?)\*\*/g, "$1")
      .replace(/__(.+?)__/g, "$1")
      .replace(/\*([^*]+)\*/g, "$1")
      .replace(/_([^_]+)_/g, "$1"))
    .map(line => stripResidualMarkdownMarkers(line))
    .join("\n");
}

async function requestChat(fetchImpl, stream, payload, options = {}) {
  const mode = detectSpinnerMode("/chat", payload);
  const spinner = createSpinner(
    stream,
    loadingLabelsFor("/chat", payload, { randomFn: options.randomFn }),
    { mode },
  );
  spinner.start();

  const response = await fetchImpl(`${BACKEND_BASE_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream, application/json",
      ...(options.headers || {}),
    },
    body: JSON.stringify({ ...payload, stream: true }),
  });

  if (!response.ok) {
    spinner.stop();
    const text = await response.text();
    throw new UserError(`Backend request failed (${response.status}): ${text}`);
  }

  const contentType = responseHeaderValue(response, "content-type");
  const canStream = contentType.includes("text/event-stream")
    && response.body
    && typeof response.body.getReader === "function";
  if (!canStream) {
    spinner.stop();
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { message: text };
      }
    }
    return data;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawChunk = false;
  let donePayload = null;
  let streamError = null;
  let renderedPrefix = false;
  let sawReasoning = false;
  let spinnerTimer = null;
  let spinnerRunning = true;
  let streamedRawText = "";
  let planSteps = [];
  let renderedPlan = false;
  let validationFlags = [];

  const clearSpinnerTimer = () => {
    if (spinnerTimer !== null) {
      clearTimeout(spinnerTimer);
      spinnerTimer = null;
    }
  };

  const stopSpinner = ({ markChunk = false } = {}) => {
    clearSpinnerTimer();
    if (markChunk) {
      sawChunk = true;
    }
    if (spinnerRunning) {
      spinner.stop();
      spinnerRunning = false;
    }
  };

  const scheduleSpinner = () => {
    if (sawChunk) {
      return;
    }
    clearSpinnerTimer();
    spinnerTimer = setTimeout(() => {
      spinner.start();
      spinnerRunning = true;
      spinnerTimer = null;
    }, 180);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    buffer = parseSseEvents(buffer, (eventName, eventPayload) => {
      if (eventName === "message" && eventPayload && typeof eventPayload.delta === "string") {
        if (!sawChunk) {
          stopSpinner({ markChunk: true });
        } else {
          clearSpinnerTimer();
        }
        renderedPrefix = renderedPrefix || sawReasoning;
        streamedRawText += eventPayload.delta;
      } else if (!sawChunk && !sawReasoning && eventName === "plan") {
        planSteps = normalizePlanSteps(eventPayload);
        if (planSteps.length > 0 && !renderedPlan) {
          stopSpinner();
          renderPlanBlock(stream, planSteps, supportsStyle(stream));
          renderedPlan = true;
        }
      } else if (!sawChunk && eventName === "step" && eventPayload && typeof eventPayload.message === "string") {
        stopSpinner();
        spinner.setLabel(eventPayload.message);
        scheduleSpinner();
      } else if (!sawChunk && eventName === "reasoning" && eventPayload && typeof eventPayload.message === "string") {
        stopSpinner();
        sawReasoning = true;
        if (planSteps.length > 0 && !renderedPlan) {
          renderPlanBlock(stream, planSteps, supportsStyle(stream));
          renderedPlan = true;
        }
        renderReasoningLine(stream, eventPayload.message, supportsStyle(stream));
        scheduleSpinner();
      } else if (eventName === "validation") {
        validationFlags = normalizeValidationFlags(eventPayload);
      } else if (eventName === "done") {
        stopSpinner();
        donePayload = eventPayload;
      } else if (eventName === "error") {
        stopSpinner();
        streamError = eventPayload;
      }
    });
    if (streamError) {
      break;
    }
  }

  clearSpinnerTimer();
  if (spinnerRunning) {
    spinner.stop();
    spinnerRunning = false;
  }
  if (streamError) {
    if (typeof reader.cancel === "function") {
      await reader.cancel();
    }
    throw new UserError(streamError.message || "Streaming chat request failed.");
  }
  if (sawChunk && donePayload && typeof donePayload.message !== "string" && streamedRawText) {
    donePayload = { ...donePayload, message: streamedRawText };
  }
  const finalPayload = { ...(donePayload || { message: streamedRawText || "" }) };
  const finalValidationFlags = normalizeValidationFlags(finalPayload);
  if (finalValidationFlags.length > 0) {
    finalPayload.validation_flags = finalValidationFlags;
  } else if (validationFlags.length > 0) {
    finalPayload.validation_flags = validationFlags;
  }
  return { ...finalPayload, __streamed: false };
}

async function requestJsonWithSpinner(fetchImpl, stream, path, payload, options = {}) {
  const mode = detectSpinnerMode(path, payload);
  const spinner = createSpinner(
    stream,
    loadingLabelsFor(path, payload, { randomFn: options.randomFn }),
    { mode },
  );
  spinner.start();
  try {
    return await requestJson(fetchImpl, path, payload, { headers: options.headers });
  } finally {
    spinner.stop();
  }
}

function extractInlineSegments(text) {
  const segments = [];
  let value = String(text || "");
  value = value.replace(/`([^`]+)`/g, (_, content) => {
    const token = `\u0000${segments.length}\u0000`;
    segments.push({ token, text: content, style: "code" });
    return token;
  });
  value = value.replace(/\*\*(.+?)\*\*/g, (_, content) => {
    const token = `\u0000${segments.length}\u0000`;
    segments.push({ token, text: content, style: "bold" });
    return token;
  });
  value = value.replace(/\[(.+?)\]\((.+?)\)/g, "$1");
  value = value.replace(/\*([^*]+)\*/g, "$1");
  value = stripResidualMarkdownMarkers(value);
  return { value, segments };
}

function restoreInlineSegments(text, segments, styled) {
  let value = text;
  for (const segment of segments) {
    const rendered = segment.style === "code"
      ? stylize(segment.text, ANSI_CYAN, styled, { bold: true })
      : stylize(segment.text, ANSI_WHITE, styled, { bold: true });
    value = value.replace(segment.token, rendered);
  }
  return value;
}

function formatMarkdownMessage(message, options = {}) {
  const styled = Boolean(options.styled);
  const lines = String(message || "").split(/\r?\n/);

  return lines
    .map(line => {
      const trimmed = line.trim();
      if (!trimmed) {
        return "";
      }

      const { value, segments } = extractInlineSegments(trimmed);

      const headingMatch = value.match(/^#{1,6}\s*(.+)$/);
      if (headingMatch) {
        return stylize(restoreInlineSegments(headingMatch[1], segments, styled), ANSI_YELLOW, styled, { bold: true });
      }

      const bulletMatch = value.match(/^[-*]\s+(.+)$/);
      if (bulletMatch) {
        return `${stylize("•", ANSI_CYAN, styled, { bold: true })} ${restoreInlineSegments(bulletMatch[1], segments, styled)}`;
      }

      const numberedMatch = value.match(/^(\d+)\.\s+(.+)$/);
      if (numberedMatch) {
        return `${stylize(`${numberedMatch[1]}.`, ANSI_CYAN, styled, { bold: true })} ${restoreInlineSegments(numberedMatch[2], segments, styled)}`;
      }

      return restoreInlineSegments(value, segments, styled);
    })
    .join("\n");
}

function normalizePlanSteps(payload) {
  if (!payload || typeof payload !== "object") {
    return [];
  }

  const candidates = Array.isArray(payload.steps)
    ? payload.steps
    : Array.isArray(payload.plan)
      ? payload.plan
      : [];

  return candidates
    .map(step => typeof step === "string" ? step.trim() : "")
    .filter(Boolean);
}

function normalizeValidationFlags(payload) {
  const items = Array.isArray(payload)
    ? payload
    : payload && Array.isArray(payload.validation_flags)
      ? payload.validation_flags
      : [];

  return items
    .map(item => {
      if (typeof item === "string") {
        return item.trim();
      }
      if (item && typeof item === "object" && typeof item.message === "string") {
        return item.message.trim();
      }
      return "";
    })
    .filter(Boolean);
}

function renderPlanBlock(output, steps, styled) {
  if (!Array.isArray(steps) || steps.length === 0) {
    return;
  }

  const width = getWrapWidth(output);
  const heading = stylize("Prophet is planning...", ANSI_GRAY, styled, { dim: true });
  const renderedSteps = steps
    .map((step, index) => wrapText(step, width, {
      firstIndent: `  ${index + 1}. `,
      restIndent: "     ",
    }))
    .map(text => stylize(text, ANSI_GRAY, styled, { dim: true }))
    .join("\n");

  writeLine(output, `\n${heading}\n\n${renderedSteps}\n\n`);
}

function renderValidationFlags(consoleLike, flags, options = {}) {
  if (!Array.isArray(flags) || flags.length === 0) {
    return;
  }

  const styled = Boolean(options.styled);
  const width = getWrapWidth(options.output);
  const prefixPlain = "⚠  ";
  const prefix = stylize(prefixPlain, ANSI_YELLOW, styled, { bold: true });
  const continuation = "   ";
  const rendered = flags
    .map(flag => wrapText(flag, width, {
      firstIndent: prefixPlain,
      restIndent: continuation,
    })
      .split("\n")
      .map((line, index) => {
        if (!styled) {
          return line;
        }
        if (index === 0 && line.startsWith(prefixPlain)) {
          return `${prefix}${stylize(line.slice(prefixPlain.length), ANSI_YELLOW, styled)}`;
        }
        return stylize(line, ANSI_YELLOW, styled);
      })
      .join("\n"))
    .join("\n");

  consoleLike.log(`\n${rendered}`);
}

function renderHelpMenu(consoleLike, commands, options = {}) {
  if (!Array.isArray(commands) || commands.length === 0) {
    return;
  }

  const styled = Boolean(options.styled);
  const width = getWrapWidth(options.output);
  for (const entry of commands) {
    const [command, description] = Array.isArray(entry) ? entry : [];
    if (!command || !description) {
      continue;
    }
    const commandText = stylize(command.padEnd(13), ANSI_CYAN, styled, { bold: true });
    const firstIndent = `  ${commandText} `;
    const restIndent = " ".repeat(visibleLength(firstIndent));
    const line = `${firstIndent}${description}`;
    consoleLike.log(visibleLength(line) <= width
      ? line
      : wrapText(description, width, {
        firstIndent,
        restIndent,
      }));
  }
}

function renderModelPicker(consoleLike, metadata, options = {}) {
  if (!metadata || typeof metadata !== "object") {
    return;
  }

  const styled = Boolean(options.styled);
  const width = getWrapWidth(options.output);
  if (metadata.current) {
    const line = `${stylize("Current model:", ANSI_YELLOW, styled, { bold: true })} ${metadata.current}`;
    consoleLike.log(visibleLength(line) <= width ? line : wrapText(line, width));
  }
  if (!Array.isArray(metadata.options)) {
    return;
  }

  for (const option of metadata.options) {
    const [name, detail, note] = Array.isArray(option) ? option : [];
    if (!name || !detail) {
      continue;
    }
    const detailLine = `  ${stylize(name.padEnd(8), ANSI_CYAN, styled, { bold: true })} ${detail}`;
    consoleLike.log(visibleLength(detailLine) <= width
      ? detailLine
      : wrapText(`${stylize(name.padEnd(8), ANSI_CYAN, styled, { bold: true })} ${detail}`, width, {
        firstIndent: "  ",
        restIndent: "           ",
      }));
    if (note) {
      const noteLine = `           ${stylize(note, ANSI_GRAY, styled, { dim: true })}`;
      consoleLike.log(visibleLength(noteLine) <= width
        ? noteLine
        : wrapText(`${stylize(note, ANSI_GRAY, styled, { dim: true })}`, width, {
          firstIndent: "           ",
          restIndent: "           ",
        }));
    }
  }
}

function renderChatResponse(consoleLike, data, options = {}) {
  const styled = Boolean(options.styled);
  const prefix = stylize("Prophet>", ANSI_YELLOW, styled, { bold: true });
  const width = getWrapWidth(options.output);
  const indent = " ".repeat(visibleLength(prefix) + 1);
  const wrapped = wrapText(stripMarkdownSyntax(data.message), width, {
    firstIndent: `${prefix} `,
    restIndent: indent,
  });
  consoleLike.log(`\n${wrapped}\n`);

  const view = data && data.metadata ? data.metadata.view : null;
  if (view === "help_menu") {
    renderHelpMenu(consoleLike, data.metadata.commands, { styled, output: options.output });
    consoleLike.log("");
    return;
  }
  if (view === "model_picker") {
    renderModelPicker(consoleLike, data.metadata, { styled, output: options.output });
    consoleLike.log("");
  }

  renderValidationFlags(consoleLike, normalizeValidationFlags(data), {
    styled,
    output: options.output,
  });
}

function renderStreamedPrefix(output, styled) {
  const prefix = stylize("Prophet>", ANSI_YELLOW, styled, { bold: true });
  writeLine(output, `\n${prefix} `);
  return visibleLength(prefix) + 1;
}

function renderReasoningLine(output, message, styled) {
  const text = String(message || "").trim();
  if (!text) {
    return;
  }
  const width = getWrapWidth(output);
  const bullet = stylize("◆", ANSI_GRAY, styled, { bold: true });
  const bulletPlain = "◆";
  const wrappedLines = wrapText(text, width, {
    firstIndent: `${bulletPlain} `,
    restIndent: "  ",
  })
    .split("\n");
  if (styled && wrappedLines.length > 0) {
    const firstBody = wrappedLines[0].slice(bulletPlain.length + 1);
    wrappedLines[0] = `${bullet} ${stylize(firstBody, ANSI_GRAY, styled, { dim: true })}`;
  }
  const rendered = wrappedLines.map((line, index) => (styled && index === 0
    ? line
    : stylize(line, ANSI_GRAY, styled, { dim: true })));
  writeLine(output, `${rendered.join("\n")}\n`);
}

function renderPostStreamResponse(consoleLike, data, options = {}) {
  const view = data && data.metadata ? data.metadata.view : null;
  const styled = Boolean(options.styled);
  if (view === "help_menu") {
    renderHelpMenu(consoleLike, data.metadata.commands, { styled, output: options.output });
    consoleLike.log("");
  }
  if (view === "model_picker") {
    renderModelPicker(consoleLike, data.metadata, { styled, output: options.output });
    consoleLike.log("");
  }
}

async function runChat(consoleLike, fetchImpl, overrides, initialMessage) {
  let sessionId = null;
  let history = [];
  const output = overrides.stdout || process.stdout;
  const input = overrides.stdin || process.stdin;
  const config = overrides.config || configStore;
  const requestHeaders = () => buildDeviceHeaders(config);
  const styled = supportsStyle(output);
  let activeRl = null;

  const recordTurn = (userMessage, response) => {
    history.push({ role: "user", content: userMessage, metadata: {} });
    history.push({
      role: "assistant",
      content: response && typeof response.message === "string" ? response.message : "",
      metadata: response && response.metadata ? response.metadata : {},
    });
  };

  const buildPayload = message => ({
    message,
    session_id: sessionId,
    history: [...history, { role: "user", content: message, metadata: {} }],
  });

  const preprocessChatMessage = message => {
    const trimmed = message.trim();
    const imagePaths = detectImagePaths(trimmed);

    if (imagePaths.length === 0) {
      return { ok: true, message: trimmed, payload: {} };
    }

    if (imagePaths.length > 2) {
      consoleLike.log([
        "Prophet supports up to 2 chart images per message.",
        "Please send one or two charts at a time.",
      ].join("\n"));
      return { ok: false };
    }

    const attachments = [];
    for (const filePath of imagePaths) {
      const validation = validateImageFile(filePath);
      if (!validation.valid) {
        consoleLike.log(validation.error);
        return { ok: false };
      }

      const filename = filePath.split(/[\\/]/).at(-1) || filePath;
      consoleLike.log(`Reading chart: ${filename}...`);

      let imageB64;
      try {
        imageB64 = readImageAsBase64(filePath);
      } catch {
        consoleLike.log("Could not read image file. Check the file path and try again.");
        return { ok: false };
      }

      if (!imageB64) {
        consoleLike.log("Could not read image file. Check the file path and try again.");
        return { ok: false };
      }

      attachments.push({
        base64: imageB64,
        mediaType: validation.mediaType,
      });
    }

    const cleanedMessage = stripImagePathsFromMessage(trimmed, imagePaths);
    const fallbackMessage = attachments.length === 2
      ? "Analyse these attached charts using my PROPHET.md rules as a multi-timeframe setup."
      : "Analyse the attached chart using my PROPHET.md rules.";
    const prompt = cleanedMessage || fallbackMessage;
    const payload = {
      image_b64: attachments[0].base64,
      media_type: attachments[0].mediaType,
    };
    if (attachments[1]) {
      payload.image_b64_2 = attachments[1].base64;
      payload.media_type_2 = attachments[1].mediaType;
    }

    return {
      ok: true,
      message: prompt,
      payload,
    };
  };

  const resumeSession = async sessionRef => {
    const payload = await requestJson(fetchImpl, `/sessions/resume/${encodeURIComponent(sessionRef)}`, null, {
      method: "POST",
      headers: requestHeaders(),
    });
    sessionId = payload.id || sessionRef;
    history = Array.isArray(payload.messages) ? payload.messages.map(item => ({
      role: item.role,
      content: item.content,
      metadata: item.metadata || {},
    })) : [];
    if (payload.recap) {
      renderChatResponse(consoleLike, { message: payload.recap, metadata: {} }, { styled, output });
    }
    return payload;
  };

  const resumeLatestSession = async () => {
    const sessions = await requestGetJson(fetchImpl, "/sessions", null, { headers: requestHeaders() });
    if (!Array.isArray(sessions) || sessions.length === 0) {
      consoleLike.log(stylize("No saved sessions found. Starting a new chat.", ANSI_GRAY, styled, { dim: true }));
      return false;
    }
    await resumeSession(sessions[0].id);
    return true;
  };

  const sendChatMessage = async message => {
    const trimmed = message.trim();
    if (!trimmed) {
      return true;
    }
    if (trimmed.toLowerCase() === "exit" || trimmed.toLowerCase() === "quit") {
      return false;
    }

    const prepared = preprocessChatMessage(trimmed);
    if (!prepared.ok) {
      return true;
    }

    const data = await requestChat(fetchImpl, output, { ...buildPayload(prepared.message), ...prepared.payload }, {
      randomFn: overrides.randomFn,
      headers: requestHeaders(),
    });

    sessionId = data.session_id || sessionId;
    recordTurn(prepared.message, data);
    if (!data.__streamed) {
      renderChatResponse(consoleLike, data, { styled, output });
    } else {
      renderPostStreamResponse(consoleLike, data, { styled, output });
    }
    return !(data && data.should_exit);
  };

  const fetchCommandPreview = async command => {
    const payload = await requestJson(fetchImpl, "/chat", {
      ...buildPayload(command),
      stream: false,
    }, { headers: requestHeaders() });
    sessionId = payload.session_id || sessionId;
    return payload;
  };

  const handleInteractiveSlashCommand = async message => {
    if (!supportsInteractive(input, output)) {
      return null;
    }
    const trimmed = message.trim();
    if (!["/model", "/pairs", "/sessions", "/calendar"].includes(trimmed)) {
      return null;
    }

    const runSelector = async callback => {
      const prompts = await loadPrompts(overrides);
      const abortController = typeof AbortController === "function" ? new AbortController() : null;
      let selectorError = null;
      if (activeRl && typeof activeRl.pause === "function") {
        activeRl.pause();
      }
      if (typeof input.resume === "function") {
        input.resume();
      }
      try {
        return await callback(prompts, {
          input,
          output,
          clearPromptOnDone: true,
          signal: abortController ? abortController.signal : undefined,
        });
      } catch (error) {
        selectorError = error;
        throw error;
      } finally {
        if (abortController && selectorError) {
          abortController.abort();
        }
        if (activeRl && typeof activeRl.resume === "function") {
          activeRl.resume();
        }
      }
    };

    if (trimmed === "/sessions") {
      try {
        const sessions = await requestGetJson(fetchImpl, "/sessions", null, { headers: requestHeaders() });
        if (!Array.isArray(sessions) || sessions.length === 0) {
          renderChatResponse(consoleLike, { message: "No saved sessions yet.", metadata: {} }, { styled, output });
          return true;
        }
        const selected = await runSelector((prompts, context) => prompts.select({
          message: "Select a session to resume",
          choices: sessions.map((item, index) => ({
            name: `${index + 1}. ${item.summary || "No summary yet."}`,
            value: item.id,
          })),
        }, context));
        await resumeSession(selected);
      } catch (error) {
        if (!isPromptCancelError(error)) {
          throw error;
        }
      }
      return true;
    }

    if (trimmed === "/calendar") {
      try {
        const choices = [
          { name: "Today", value: { view: "today" } },
          { name: "This week", value: { view: "week" } },
        ];
        const selection = await runSelector((prompts, context) => prompts.select({ message: "Calendar view", choices }, context));
        const payload = await requestGetJson(fetchImpl, "/calendar", { view: selection.view }, { headers: requestHeaders() });
        const message = formatCalendarPayload(payload);
        const response = { message, metadata: { ...payload, view: "calendar_picker" } };
        history.push({ role: "user", content: "/calendar", metadata: {} });
        history.push({ role: "assistant", content: message, metadata: response.metadata });
        renderChatResponse(consoleLike, response, { styled, output });
        return true;
      } catch (error) {
        if (isPromptCancelError(error)) {
          return true;
        }
        throw error;
      }
    }

    if (trimmed === "/model") {
      try {
        const preview = await fetchCommandPreview("/model");
        const selected = await runSelector((prompts, context) => prompts.select({
          message: "Select AI model",
          choices: (preview.metadata.options || []).map(option => ({
            name: option[0] === preview.metadata.current ? `${option[0]} (current)` : option[0],
            value: option[0],
          })),
        }, context));
        const response = await requestJson(fetchImpl, "/chat", {
          ...buildPayload(`/model ${selected}`),
          stream: false,
        }, { headers: requestHeaders() });
        sessionId = response.session_id || sessionId;
        recordTurn(`/model ${selected}`, response);
        renderChatResponse(consoleLike, response, { styled, output });
      } catch (error) {
        if (!isPromptCancelError(error)) {
          throw error;
        }
      }
      return true;
    }

    if (trimmed === "/pairs") {
      try {
        const preview = await fetchCommandPreview("/pairs");
        const action = await runSelector((prompts, context) => prompts.select({
          message: "Watchlist action",
          choices: (preview.metadata.actions || []).map(item => ({ name: item, value: item })),
        }, context));
        if (action === "View current pairs") {
          renderChatResponse(consoleLike, preview, { styled, output });
          return true;
        }
        if (action === "Add a pair") {
          const pair = await runSelector((prompts, context) => prompts.input({ message: "Which pair would you like to add?" }, context));
          if (!pair || !pair.trim()) {
            return true;
          }
          const response = await requestJson(fetchImpl, "/chat", {
            ...buildPayload(`/pairs add ${pair}`),
            stream: false,
          }, { headers: requestHeaders() });
          sessionId = response.session_id || sessionId;
          recordTurn(`/pairs add ${pair}`, response);
          renderChatResponse(consoleLike, response, { styled, output });
          return true;
        }
        const pairChoices = (preview.metadata.pairs || []).map(item => ({ name: item, value: item }));
        const pair = await runSelector((prompts, context) => prompts.select({ message: "Select a pair to remove", choices: pairChoices }, context));
        const response = await requestJson(fetchImpl, "/chat", {
          ...buildPayload(`/pairs remove ${pair}`),
          stream: false,
        }, { headers: requestHeaders() });
        sessionId = response.session_id || sessionId;
        recordTurn(`/pairs remove ${pair}`, response);
        renderChatResponse(consoleLike, response, { styled, output });
      } catch (error) {
        if (!isPromptCancelError(error)) {
          throw error;
        }
        return true;
      }
      return true;
    }

    return null;
  };

  if (overrides.resumeLatest) {
    await resumeLatestSession();
  }

  if (initialMessage) {
    await sendChatMessage(initialMessage);
    return 0;
  }

  let promptVisible = false;
  consoleLike.log(stylize("Chat session starting... Type /help for commands. Type exit or quit to leave.", ANSI_GRAY, styled, { dim: true }));
  try {
    activeRl = readline.createInterface({
      input,
      output,
    });
    while (true) {
      promptVisible = true;
      let answer;
      try {
        answer = await activeRl.question("> ");
      } finally {
        promptVisible = false;
      }

      const interactiveHandled = await handleInteractiveSlashCommand(answer);
      if (interactiveHandled !== null) {
        if (!interactiveHandled) {
          break;
        }
        continue;
      }

      const shouldContinue = await sendChatMessage(answer);
      if (!shouldContinue) {
        break;
      }
    }
    return 0;
  } finally {
    if (activeRl) {
      activeRl.close();
      activeRl = null;
    }
  }
}

async function runCli(overrides = {}) {
  const consoleLike = overrides.console || global.console;
  const fetchImpl = overrides.fetch || global.fetch;
  const config = overrides.config || configStore;
  if (typeof fetchImpl !== "function") {
    throw new UserError("This runtime does not provide fetch. Use Node.js 18 or newer.");
  }

  const updateCheckPromise = startUpdateCheck({
    currentVersion: overrides.currentVersion || CLI_VERSION,
    packageName: overrides.packageName || PACKAGE_NAME,
    updateCheckInterval: overrides.updateCheckInterval,
    loadUpdateNotifier: overrides.loadUpdateNotifier,
  });

  consoleLike.log(PROPHET_BANNER);
  consoleLike.log(PROPHET_VERSION_LINE);
  const parsed = parseCommand(overrides.argv || []);
  const updateInfo = await updateCheckPromise;
  if (updateInfo) {
    consoleLike.log(formatUpdateNotification(updateInfo.currentVersion, updateInfo.latestVersion));
    if (shouldPauseForUpdateNotice(parsed)) {
      const wait = overrides.wait || pause;
      await wait(overrides.updateNotificationDelayMs ?? UPDATE_NOTIFICATION_DELAY_MS);
    }
  }
  if (parsed.command === "help") {
    consoleLike.log(formatHelpText());
    return 0;
  }

  const shouldBootstrapProfile = overrides.enableProfileBootstrap === true
    || (!overrides.fetch && config && typeof config.configExists === "function");
  if (shouldBootstrapProfile) {
    const profileState = await ensureProfile(fetchImpl, consoleLike, { ...overrides, config });
    if (profileState && profileState.status === "cancelled") {
      return 0;
    }
    if (profileState && profileState.status === "failed") {
      return 1;
    }
  }

  if (parsed.command === "chat") {
    return runChat(consoleLike, fetchImpl, { ...overrides, config }, parsed.message);
  }
  if (parsed.command === "resume") {
    return runChat(consoleLike, fetchImpl, { ...overrides, config, resumeLatest: true }, null);
  }

  const data = await requestJsonWithSpinner(
    fetchImpl,
    overrides.stdout || process.stdout,
    `/${parsed.command}`,
    parsed.payload,
    { randomFn: overrides.randomFn, headers: buildDeviceHeaders(config) },
  );
  printJson(consoleLike, data);
  return 0;
}

module.exports = {
  BACKEND_BASE_URL,
  UserError,
  createSpinner,
  detectSpinnerMode,
  formatMarkdownMessage,
  formatSpinnerText,
  formatUpdateNotification,
  formatWelcomeBackMessage,
  loadingLabelsFor,
  normalizePlanSteps,
  normalizeValidationFlags,
  parseCommand,
  requestJson,
  requestJsonWithSpinner,
  renderChatResponse,
  renderPlanBlock,
  renderReasoningLine,
  renderValidationFlags,
  runCli,
  shuffleLabels,
  startUpdateCheck,
  formatHelpText,
  ensureProfile,
  buildDeviceHeaders,
  shouldPauseForUpdateNotice,
};
