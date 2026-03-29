"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { PassThrough } = require("node:stream");
const { version: CLI_VERSION } = require("../package.json");

const {
  BACKEND_BASE_URL,
  UserError,
  detectSpinnerMode,
  formatHelpText,
  formatMarkdownMessage,
  formatSpinnerText,
  formatUpdateNotification,
  loadingLabelsFor,
  normalizePlanSteps,
  normalizeValidationFlags,
  parseCommand,
  renderChatResponse,
  renderPlanBlock,
  renderReasoningLine,
  renderValidationFlags,
  runCli,
  shuffleLabels,
  startUpdateCheck,
} = require("../lib/runner");

function createConsole() {
  return {
    messages: [],
    log(message) {
      this.messages.push(message);
    },
  };
}

function createStream({ isTTY = false, columns } = {}) {
  return {
    isTTY,
    columns,
    writes: [],
    write(chunk) {
      this.writes.push(String(chunk));
      return true;
    },
  };
}

function createFetch(response) {
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({ url, options });
    return {
      ok: response.ok ?? true,
      status: response.status ?? 200,
      headers: response.headers || { get() { return ""; } },
      body: response.body,
      async text() {
        return response.body;
      },
    };
  };

  return { calls, fetch };
}

function createJsonResponse(body, extra = {}) {
  return {
    ok: extra.ok ?? true,
    status: extra.status ?? 200,
    headers: {
      get(name) {
        if (name.toLowerCase() === "content-type") {
          return "application/json";
        }
        return "";
      },
    },
    async json() {
      return body;
    },
    async text() {
      return JSON.stringify(body);
    },
  };
}

function createSseResponse(events) {
  const chunks = events.map(event => `event: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`);
  let index = 0;
  return {
    ok: true,
    status: 200,
    headers: {
      get(name) {
        if (name.toLowerCase() === "content-type") {
          return "text/event-stream";
        }
        return "";
      },
    },
    body: {
      getReader() {
        return {
          async read() {
            if (index >= chunks.length) {
              return { done: true, value: undefined };
            }
            const value = Buffer.from(chunks[index], "utf8");
            index += 1;
            return { done: false, value };
          },
        };
      },
    },
    async text() {
      return chunks.join("");
    },
  };
}

function stripAnsi(text) {
  return String(text || "").replace(/\u001b\[[0-9;]*m/g, "");
}

function createConfigStub(initial = null) {
  let config = initial;
  return {
    clearCalls: 0,
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
      this.clearCalls += 1;
      config = null;
      return true;
    },
    isConfigValid(candidate) {
      return Boolean(candidate && candidate.device_token && candidate.onboarded === true);
    },
    getDeviceToken() {
      return this.isConfigValid(config) ? config.device_token : null;
    },
  };
}

const ONE_BY_ONE_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6W6p0AAAAASUVORK5CYII=";

function withTempDir(t) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "prophetaf-runner-"));
  t.after(() => {
    fs.rmSync(tempDir, { recursive: true, force: true });
  });
  return tempDir;
}

function writePng(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, Buffer.from(ONE_BY_ONE_PNG, "base64"));
}

async function runCliForTest(overrides = {}) {
  return runCli({
    loadUpdateNotifier: async () => () => ({ update: null }),
    wait: async () => {},
    ...overrides,
  });
}

test("parseCommand treats bare text as a chat message", () => {
  assert.deepEqual(parseCommand(["show", "xauusd", "bias"]), {
    command: "chat",
    message: "show xauusd bias",
  });
});

test("parseCommand maps scan flags to the scan payload", () => {
  assert.deepEqual(parseCommand(["scan", "--pair", "XAUUSD"]), {
    command: "scan",
    payload: { pair: "XAUUSD" },
  });
});

test("parseCommand recognizes resume mode", () => {
  assert.deepEqual(parseCommand(["resume"]), { command: "resume" });
});

test("parseCommand recognizes the help flags", () => {
  assert.deepEqual(parseCommand(["--help"]), { command: "help" });
  assert.deepEqual(parseCommand(["-h"]), { command: "help" });
});

test("parseCommand validates the risk command arguments", () => {
  assert.throws(
    () => parseCommand(["risk", "--pair", "XAUUSD", "--risk", "1"]),
    error => {
      assert.ok(error instanceof UserError);
      assert.match(error.message, /risk requires --pair, --sl, and --risk/);
      return true;
    },
  );
});

test("formatUpdateNotification matches the startup update prompt", () => {
  assert.equal(
    formatUpdateNotification("3.3.0", "3.3.1"),
    [
      "──────────────────────────────────────────────────",
      "  New version available: v3.3.1",
      "  You are running:       v3.3.0",
      "",
      "  Run the following to update:",
      "  npm install -g prophetaf@latest",
      "──────────────────────────────────────────────────",
    ].join("\n"),
  );
});

test("formatHelpText prints the supported commands", () => {
  const help = formatHelpText();

  assert.match(help, /Usage: prophetaf/);
  assert.match(help, /scan --pair PAIR/);
  assert.match(help, /-h, --help/);
});

test("startUpdateCheck reads cached update-notifier info", async () => {
  let receivedOptions = null;

  const updateInfo = await startUpdateCheck({
    currentVersion: "3.3.0",
    packageName: "prophetaf",
    loadUpdateNotifier: async () => options => {
      receivedOptions = options;
      return {
        update: {
          current: "3.3.0",
          latest: "3.3.1",
        },
      };
    },
  });

  assert.deepEqual(updateInfo, {
    currentVersion: "3.3.0",
    latestVersion: "3.3.1",
  });
  assert.equal(receivedOptions.pkg.name, "prophetaf");
  assert.equal(receivedOptions.pkg.version, "3.3.0");
  assert.equal(receivedOptions.updateCheckInterval, 1000 * 60 * 60);
});

test("startUpdateCheck swallows update-notifier failures", async () => {
  const updateInfo = await startUpdateCheck({
    loadUpdateNotifier: async () => {
      throw new Error("offline");
    },
  });

  assert.equal(updateInfo, null);
});

test("detectSpinnerMode only uses web mode for explicit or event-driven live queries", () => {
  assert.equal(detectSpinnerMode("/chat", { message: "Search the web for Gold headlines" }), "web");
  assert.equal(detectSpinnerMode("/chat", { message: "Will Gold react to CPI today?" }), "web");
  assert.equal(detectSpinnerMode("/chat", { message: "What are today's key levels for Gold?" }), "chat");
  assert.equal(detectSpinnerMode("/chat", { message: "Give me the latest structure on XAUUSD" }), "chat");
  assert.equal(detectSpinnerMode("/chat", { message: "/model" }), "command");
});

test("loadingLabelsFor shuffles every mode label set", () => {
  const labels = loadingLabelsFor("/scan", {}, { randomFn: () => 0 });

  assert.equal(labels.length, 10);
  assert.equal(new Set(labels).size, 10);
  assert.notEqual(labels[0], "Sweeping the watchlist...");
});

test("loadingLabelsFor returns dedicated web-search labels", () => {
  const labels = loadingLabelsFor("/chat", { message: "Search the web for Gold headlines" }, { randomFn: () => 0 });

  assert.equal(labels.length, 10);
  assert.ok(labels.includes("Searching the web..."));
  assert.ok(labels.includes("Scanning live headlines..."));
});

test("shuffleLabels preserves all labels", () => {
  const labels = ["a", "b", "c", "d"];
  const shuffled = shuffleLabels(labels, () => 0.25);

  assert.deepEqual([...shuffled].sort(), [...labels].sort());
});

test("formatSpinnerText adds design accents for tty output", () => {
  const text = formatSpinnerText("◐", "Searching the web...", { frame: "\u001b[34m", label: "\u001b[33m" }, true);

  assert.match(text, /\u001b\[34m/);
  assert.match(text, /Searching the web/);
  assert.match(text, /Prophet is working/);
});

test("formatMarkdownMessage strips raw markdown markers into readable terminal text", () => {
  const formatted = formatMarkdownMessage(
    "### **Market Context**\n* **Bias:** Bullish\n* **News:** Check `headlines`\n**Risk Note:** Stay patient",
    { styled: false },
  );

  assert.equal(
    formatted,
    ["Market Context", "• Bias: Bullish", "• News: Check headlines", "Risk Note: Stay patient"].join("\n"),
  );
  assert.doesNotMatch(formatted, /\*\*|###/);
});

test("formatMarkdownMessage handles adjacent bold and italic spans safely", () => {
  const formatted = formatMarkdownMessage("**Bias** *supports* continuation", { styled: false });

  assert.equal(formatted, "Bias supports continuation");
});

test("formatMarkdownMessage strips stray bold markers attached to words", () => {
  const formatted = formatMarkdownMessage("This is abc**notrecommended and **rket structure", { styled: false });

  assert.equal(formatted, "This is abcnotrecommended and rket structure");
  assert.doesNotMatch(formatted, /\*\*/);
});

test("runCli sends one-off chat messages to the live backend URL", async () => {
  const fakeConsole = createConsole();
  const stream = createStream();
  const { calls, fetch } = createFetch({
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    body: JSON.stringify({ message: "Hello from Prophet", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdin: process.stdin,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${BACKEND_BASE_URL}/chat`);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    message: "hello there",
    session_id: null,
    history: [{ role: "user", content: "hello there", metadata: {} }],
    stream: true,
  });
  assert.match(fakeConsole.messages[2], /Prophet> Hello from Prophet/);
});

test("runCli attaches one chart image and strips the path from the request body", async t => {
  const tempDir = withTempDir(t);
  const imagePath = path.join(tempDir, "gold chart.png");
  writePng(imagePath);
  const fakeConsole = createConsole();
  const stream = createStream();
  const { calls, fetch } = createFetch({
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    body: JSON.stringify({ message: "Chart processed", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: [`what do you see in this chart: "${imagePath}"`],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.message, "what do you see in this chart:");
  assert.deepEqual(payload.history, [{ role: "user", content: "what do you see in this chart:", metadata: {} }]);
  assert.equal(payload.media_type, "image/png");
  assert.ok(typeof payload.image_b64 === "string" && payload.image_b64.length > 0);
  assert.equal(payload.image_b64_2, undefined);
  assert.ok(fakeConsole.messages.some(message => message.includes("Reading chart: gold chart.png...")));
});

test("runCli attaches two chart images for multi-timeframe analysis", async t => {
  const tempDir = withTempDir(t);
  const h1Path = path.join(tempDir, "xauusd-h1.png");
  const m15Path = path.join(tempDir, "xauusd-m15.png");
  writePng(h1Path);
  writePng(m15Path);
  const { calls, fetch } = createFetch({
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    body: JSON.stringify({ message: "Aligned", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: [`analyse both "${h1Path}" "${m15Path}" for confluence`],
    console: createConsole(),
    fetch,
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.message, "analyse both for confluence");
  assert.equal(payload.media_type, "image/png");
  assert.equal(payload.media_type_2, "image/png");
  assert.ok(typeof payload.image_b64 === "string" && payload.image_b64.length > 0);
  assert.ok(typeof payload.image_b64_2 === "string" && payload.image_b64_2.length > 0);
});

test("runCli falls back to the default chart prompt when the message only contains one image path", async t => {
  const tempDir = withTempDir(t);
  const imagePath = path.join(tempDir, "chart.png");
  writePng(imagePath);
  const { calls, fetch } = createFetch({
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    body: JSON.stringify({ message: "Chart processed", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: [`"${imagePath}"`],
    console: createConsole(),
    fetch,
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.message, "Analyse the attached chart using my PROPHET.md rules.");
  assert.deepEqual(payload.history, [{
    role: "user",
    content: "Analyse the attached chart using my PROPHET.md rules.",
    metadata: {},
  }]);
});

test("runCli rejects more than two chart images in a single message", async () => {
  const fakeConsole = createConsole();
  const { calls, fetch } = createFetch({
    body: JSON.stringify({ message: "unused", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: ["analyse ./one.png ./two.png ./three.png"],
    console: fakeConsole,
    fetch,
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 0);
  assert.ok(fakeConsole.messages.some(message => /Prophet supports up to 2 chart images per message\./.test(message)));
});

test("runCli reports invalid chart files without calling the backend", async t => {
  const tempDir = withTempDir(t);
  const filePath = path.join(tempDir, "missing.png");
  const fakeConsole = createConsole();
  const { calls, fetch } = createFetch({
    body: JSON.stringify({ message: "unused", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: [`analyse "${filePath}"`],
    console: fakeConsole,
    fetch,
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 0);
  assert.ok(fakeConsole.messages.some(message => message.includes(`Image not found or format not supported: ${filePath}`)));
});

test("runCli streams chat chunks when the backend returns SSE", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    { event: "step", data: { message: "Scanning the watchlist..." } },
    { event: "reasoning", data: { message: "XAUUSD has the strongest sweep so far." } },
    { event: "message", data: { delta: "**Hello** " } },
    { event: "message", data: { delta: "from Prophet" } },
    { event: "reasoning", data: { message: "This should not render after the answer starts." } },
    { event: "done", data: { message: "Hello from Prophet", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.ok(stream.writes.some(chunk => chunk.includes("◆")));
  assert.ok(stream.writes.some(chunk => chunk.includes("XAUUSD has the strongest sweep so far.")));
  assert.ok(!stream.writes.some(chunk => chunk.includes("This should not render after the answer starts.")));
  assert.match(stripAnsi(fakeConsole.messages[2]), /Prophet> Hello from Prophet/);
  assert.doesNotMatch(stripAnsi(fakeConsole.messages[2]), /\*\*/);
});

test("runCli renders a plan block before the first reasoning line", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    {
      event: "plan",
      data: {
        steps: [
          "Fetch H1 bias for EURUSD and XAUUSD",
          "Scan M15 setups for both pairs",
          "Check economic calendar for USD events today",
        ],
      },
    },
    { event: "reasoning", data: { message: "Fetching H1 bias for EURUSD..." } },
    { event: "message", data: { delta: "Bias is aligned." } },
    { event: "done", data: { message: "Bias is aligned.", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  const rendered = stripAnsi(stream.writes.join(""));
  const planIndex = rendered.indexOf("Prophet is planning...");
  const reasoningIndex = rendered.indexOf("◆ Fetching H1 bias for EURUSD...");
  assert.ok(planIndex >= 0);
  assert.ok(reasoningIndex > planIndex);
});

test("runCli renders a plan block before streamed answer text when message chunks arrive first", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    {
      event: "plan",
      data: {
        steps: [
          "Check EURUSD structure",
          "Check XAUUSD structure",
        ],
      },
    },
    { event: "message", data: { delta: "Best setup is XAUUSD." } },
    { event: "done", data: { message: "Best setup is XAUUSD.", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  const rendered = stripAnsi(stream.writes.join(""));
  const planIndex = rendered.indexOf("Prophet is planning...");
  assert.ok(planIndex >= 0);
});

test("runCli ignores plan events that arrive after answer text starts", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    { event: "message", data: { delta: "Main answer already started. " } },
    { event: "plan", data: { steps: ["This should not render."] } },
    { event: "done", data: { message: "Main answer already started.", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.doesNotMatch(stripAnsi(stream.writes.join("")), /Prophet is planning/);
});

test("runCli strips stray bold markers from streamed chunks", async () => {
  const stream = createStream({ isTTY: true });
  const fakeConsole = createConsole();
  const fetch = async () => createSseResponse([
    { event: "message", data: { delta: "This is abc**notrecommended and " } },
    { event: "message", data: { delta: "**rket structure" } },
    { event: "done", data: { message: "This is abcnotrecommended and rket structure", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  const rendered = fakeConsole.messages[2];
  assert.match(rendered, /abcnotrecommended and rket structure/);
  assert.doesNotMatch(rendered, /\*\*/);
});

test("runCli renders the final streamed response without collapsing spaces", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    { event: "reasoning", data: { message: "Checking session timing." } },
    { event: "message", data: { delta: "No. " } },
    { event: "message", data: { delta: "The market is currently closed. " } },
    { event: "message", data: { delta: "Asia opens soon." } },
    { event: "done", data: { message: "No. The market is currently closed. Asia opens soon.", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.match(fakeConsole.messages[2], /No\. The market is currently closed\. Asia opens soon\./);
});

test("runCli renders validation flags from the done payload after the main answer", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    { event: "message", data: { delta: "Main analysis text." } },
    {
      event: "done",
      data: {
        message: "Main analysis text.",
        session_id: "abc123",
        metadata: {},
        validation_flags: [
          "Rule check: You do not trade during Asia session.",
          { message: "Asia session is currently active. Wait for London open." },
        ],
      },
    },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.match(fakeConsole.messages[2], /Main analysis text\./);
  assert.match(stripAnsi(fakeConsole.messages[3]), /⚠  Rule check: You do not trade during Asia session\./);
  assert.match(stripAnsi(fakeConsole.messages[3]), /Asia session is currently active\. Wait for London open\./);
});

test("runCli falls back to validation events when done payload has none", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    { event: "validation", data: { validation_flags: ["Rule check: Wait for London open."] } },
    { event: "message", data: { delta: "Main analysis text." } },
    { event: "done", data: { message: "Main analysis text.", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.match(stripAnsi(fakeConsole.messages[3]), /⚠  Rule check: Wait for London open\./);
});

test("runCli prints help for --help", async () => {
  const fakeConsole = createConsole();

  const exitCode = await runCliForTest({
    argv: ["--help"],
    console: fakeConsole,
    fetch: async () => {
      throw new Error("backend should not be called");
    },
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.match(fakeConsole.messages[2], /Usage: prophetaf/);
});

test("renderReasoningLine prints the muted narration bullet", () => {
  const stream = createStream({ isTTY: true });

  renderReasoningLine(stream, "Gold is leading the watchlist right now.", true);

  assert.ok(stream.writes[0].includes("◆"));
  assert.ok(stream.writes[0].includes("Gold is leading the watchlist right now."));
});

test("renderReasoningLine wraps to the available terminal width", () => {
  const stream = createStream({ columns: 24 });

  renderReasoningLine(stream, "Gold is leading the watchlist with a clean sweep into support.", false);

  const lines = stripAnsi(stream.writes.join("")).trimEnd().split("\n");
  assert.ok(lines.every(line => line.length <= 24));
});

test("renderReasoningLine preserves dim gray styling after the bullet", () => {
  const stream = createStream({ isTTY: true, columns: 80 });

  renderReasoningLine(stream, "Gold is leading the watchlist right now.", true);

  const rendered = stream.writes.join("");
  assert.match(rendered, /\u001b\[1m\u001b\[90m◆\u001b\[0m \u001b\[2m\u001b\[90mGold is leading the watchlist right now\.\u001b\[0m/);
});

test("normalizePlanSteps supports steps and plan arrays", () => {
  assert.deepEqual(
    normalizePlanSteps({ steps: [" Fetch H1 bias ", "", "Scan M15 setups"] }),
    ["Fetch H1 bias", "Scan M15 setups"],
  );
  assert.deepEqual(
    normalizePlanSteps({ plan: ["Check calendar"] }),
    ["Check calendar"],
  );
});

test("normalizeValidationFlags supports strings and message objects", () => {
  assert.deepEqual(
    normalizeValidationFlags({
      validation_flags: [" Rule check: Wait for London open. ", { message: "Asia session is active." }],
    }),
    ["Rule check: Wait for London open.", "Asia session is active."],
  );
});

test("renderPlanBlock prints a muted numbered plan with spacing", () => {
  const stream = createStream({ isTTY: true, columns: 80 });

  renderPlanBlock(stream, [
    "Fetch H1 bias for EURUSD and XAUUSD",
    "Scan M15 setups for both pairs",
  ], true);

  const rendered = stripAnsi(stream.writes.join(""));
  assert.match(rendered, /\nProphet is planning\.\.\.\n\n  1\. Fetch H1 bias for EURUSD and XAUUSD\n  2\. Scan M15 setups for both pairs\n\n$/);
});

test("renderPlanBlock wraps long steps under the numbered indent", () => {
  const stream = createStream({ columns: 34 });

  renderPlanBlock(stream, [
    "Check economic calendar for USD events today before London open.",
  ], false);

  const lines = stripAnsi(stream.writes.join("")).trimEnd().split("\n").filter(Boolean);
  assert.equal(lines[0], "Prophet is planning...");
  assert.equal(lines[1], "  1. Check economic calendar for");
  assert.equal(lines[2], "     USD events today before");
  assert.equal(lines[3], "     London open.");
});

test("renderValidationFlags prints amber-style warning blocks with aligned wraps", () => {
  const fakeConsole = createConsole();

  renderValidationFlags(fakeConsole, [
    "Rule check: You do not trade during Asia session. Asia session is currently active. Wait for London open.",
  ], {
    styled: false,
    output: { columns: 44 },
  });

  const lines = fakeConsole.messages[0].split("\n");
  assert.equal(lines[0], "");
  assert.equal(lines[1], "⚠  Rule check: You do not trade during Asia");
  assert.equal(lines[2], "   session. Asia session is currently");
  assert.equal(lines[3], "   active. Wait for London open.");
});

test("runCli renders post-stream help metadata after a streamed reply", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const fetch = async () => createSseResponse([
    { event: "message", data: { delta: "Done." } },
    {
      event: "done",
      data: {
        message: "Done.",
        session_id: "abc123",
        metadata: {
          view: "help_menu",
          commands: [["/help", "Show help"]],
        },
      },
    },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.ok(fakeConsole.messages.some(message => message.includes("/help")));
});

test("runCli wraps streamed output to the terminal width", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true, columns: 36 });
  const fetch = async () => createSseResponse([
    { event: "reasoning", data: { message: "Scanning the broader market context for the cleanest setup." } },
    { event: "message", data: { delta: "Prophet keeps the focus on disciplined execution during high-volatility windows." } },
    { event: "done", data: { message: "Prophet keeps the focus on disciplined execution during high-volatility windows.", session_id: "abc123", metadata: {} } },
  ]);

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  const lines = stripAnsi(stream.writes.join(""))
    .split(/\r?\n/)
    .filter(line => line.trim().length > 0 && !line.includes("Prophet is working"));
  assert.ok(lines.every(line => line.length <= 36));
});

test("runCli stops cleanly when the SSE stream reports an error", async () => {
  await assert.rejects(
    () =>
      runCliForTest({
        argv: ["hello there"],
        console: createConsole(),
        fetch: async () => createSseResponse([
          { event: "message", data: { delta: "Hello " } },
          { event: "error", data: { message: "stream failed" } },
        ]),
        stdout: createStream({ isTTY: true }),
      }),
    error => {
      assert.ok(error instanceof UserError);
      assert.match(error.message, /stream failed/);
      return true;
    },
  );
});

test("runCli prints JSON for scan responses", async () => {
  const fakeConsole = createConsole();
  const stream = createStream();
  const { calls, fetch } = createFetch({
    body: JSON.stringify([{ pair: "XAUUSD", bias: "bullish" }]),
  });

  const exitCode = await runCliForTest({
    argv: ["scan", "--pair", "XAUUSD"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.equal(calls[0].url, `${BACKEND_BASE_URL}/scan`);
  assert.deepEqual(JSON.parse(calls[0].options.body), { pair: "XAUUSD" });
  assert.match(fakeConsole.messages[2], /"pair": "XAUUSD"/);
});

test("runCli prints JSON for risk responses", async () => {
  const fakeConsole = createConsole();
  const stream = createStream();
  const { calls, fetch } = createFetch({
    body: JSON.stringify({ pair: "XAUUSD", units: 0.67 }),
  });

  const exitCode = await runCliForTest({
    argv: ["risk", "--pair", "XAUUSD", "--sl", "15", "--risk", "1"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.equal(calls[0].url, `${BACKEND_BASE_URL}/risk`);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    pair: "XAUUSD",
    sl: 15,
    risk: 1,
  });
  assert.match(fakeConsole.messages[2], /"units": 0.67/);
});

test("runCli surfaces backend failures clearly", async () => {
  const { fetch } = createFetch({
    ok: false,
    status: 500,
    body: "Internal Server Error",
  });

  await assert.rejects(
    () =>
      runCliForTest({
        argv: ["bias", "--pair", "XAUUSD"],
        console: createConsole(),
        fetch,
        stdout: createStream(),
      }),
    error => {
      assert.ok(error instanceof UserError);
      assert.match(error.message, /Backend request failed \(500\): Internal Server Error/);
      return true;
    },
  );
});

test("runCli continues normally when update-notifier has no cached update", async () => {
  const { calls, fetch } = createFetch({
    body: JSON.stringify({ message: "Hello from Prophet", session_id: "abc123" }),
  });

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: createConsole(),
    fetch,
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 1);
});

test("runCli resumes the latest saved session before opening chat", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  const stdin = new PassThrough();
  stdin.isTTY = true;
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === `${BACKEND_BASE_URL}/sessions`) {
      return createJsonResponse([{ id: "sess-1", summary: "Friday gold session" }]);
    }
    if (url === `${BACKEND_BASE_URL}/sessions/resume/sess-1`) {
      return createJsonResponse({
        id: "sess-1",
        recap: "Resuming session from Friday.",
        messages: [{ role: "assistant", content: "Earlier answer", metadata: {} }],
      });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const runPromise = runCliForTest({
    argv: ["resume"],
    console: fakeConsole,
    fetch,
    stdin,
    stdout,
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.equal(calls[0].url, `${BACKEND_BASE_URL}/sessions`);
  assert.equal(calls[1].url, `${BACKEND_BASE_URL}/sessions/resume/sess-1`);
  assert.match(fakeConsole.messages[2], /Resuming session from Friday/);
});

test("runCli shows the startup update block and waits when a newer version is cached", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  stdout.writes = [];
  const originalWrite = stdout.write.bind(stdout);
  stdout.write = chunk => {
    stdout.writes.push(String(chunk));
    return originalWrite(chunk);
  };

  const stdin = new PassThrough();
  const waits = [];

  const runPromise = runCli({
    argv: [],
    console: fakeConsole,
    fetch: async () => {
      throw new Error("backend should not be called");
    },
    loadUpdateNotifier: async () => () => ({
      update: {
        current: "3.3.2",
        latest: "3.3.3",
      },
    }),
    wait: async ms => {
      waits.push(ms);
    },
    stdin,
    stdout,
    currentVersion: "3.3.2",
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.match(fakeConsole.messages[1], new RegExp(`Personal AI Trading Assistant \\| v${CLI_VERSION.replace(/\./g, "\\.")} \\| Cloud Edition`));
  assert.equal(
    fakeConsole.messages[2],
    [
      "──────────────────────────────────────────────────",
      "  New version available: v3.3.3",
      "  You are running:       v3.3.2",
      "",
      "  Run the following to update:",
      "  npm install -g prophetaf@latest",
      "──────────────────────────────────────────────────",
    ].join("\n"),
  );
  assert.deepEqual(waits, [2000]);
});

test("runCli shows cached update info without pausing --help", async () => {
  const fakeConsole = createConsole();
  const waits = [];

  const exitCode = await runCli({
    argv: ["--help"],
    console: fakeConsole,
    fetch: async () => {
      throw new Error("backend should not be called");
    },
    loadUpdateNotifier: async () => () => ({
      update: {
        current: "3.3.10",
        latest: "3.3.11",
      },
    }),
    wait: async ms => {
      waits.push(ms);
    },
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(waits.length, 0);
  assert.equal(
    fakeConsole.messages[2],
    [
      "──────────────────────────────────────────────────",
      "  New version available: v3.3.11",
      "  You are running:       v3.3.10",
      "",
      "  Run the following to update:",
      "  npm install -g prophetaf@latest",
      "──────────────────────────────────────────────────",
    ].join("\n"),
  );
  assert.match(fakeConsole.messages[3], /Usage: prophetaf/);
});

test("runCli uses a selector for /model in tty mode", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  stdout.writes = [];
  const originalWrite = stdout.write.bind(stdout);
  stdout.write = chunk => {
    stdout.writes.push(String(chunk));
    return originalWrite(chunk);
  };
  const stdin = new PassThrough();
  stdin.isTTY = true;
  const calls = [];
  const prompts = {
    async select() {
      return "openai";
    },
  };
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    const message = JSON.parse(options.body).message;
    return createJsonResponse(
      message === "/model"
        ? {
            message: "Choose the active model for this session.",
            session_id: "abc123",
            metadata: {
              view: "model_picker",
              current: "auto",
              options: [
                ["auto", "Gemini -> OpenAI fallback", "Best default"],
                ["openai", "gpt-5-mini", "Use OpenAI only"],
              ],
            },
          }
        : message === "/model openai"
          ? {
              message: "Model switched to openai for this session.",
              session_id: "abc123",
              metadata: {
                view: "model_picker",
                current: "openai",
                options: [
                  ["auto", "Gemini -> OpenAI fallback", "Best default"],
                  ["openai", "gpt-5-mini", "Use OpenAI only"],
                ],
              },
            }
        : {
            message: "EURUSD is bullish.",
            session_id: "abc123",
            metadata: {},
          },
    );
  };

  const runPromise = runCliForTest({
    argv: [],
    console: fakeConsole,
    fetch,
    stdin,
    stdout,
    prompts,
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/model\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("What is the current trend of EURUSD?\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 3);
  assert.equal(JSON.parse(calls[0].options.body).message, "/model");
  assert.equal(JSON.parse(calls[1].options.body).message, "/model openai");
  assert.equal(JSON.parse(calls[2].options.body).message, "What is the current trend of EURUSD?");
  assert.ok(stdout.writes.filter(chunk => chunk.includes("> ")).length >= 2);
  assert.deepEqual(JSON.parse(calls[0].options.body).history, [{ role: "user", content: "/model", metadata: {} }]);
  assert.deepEqual(JSON.parse(calls[1].options.body).history, [{ role: "user", content: "/model openai", metadata: {} }]);
});

test("runCli uses a selector for /pairs in tty mode", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  const stdin = new PassThrough();
  stdin.isTTY = true;
  const calls = [];
  const prompts = {
    async select(options) {
      if (options.message === "Watchlist action") {
        return "Add a pair";
      }
      return "EURUSD";
    },
    async input() {
      return "USDCAD";
    },
  };
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return createJsonResponse(
      JSON.parse(options.body).message === "/pairs"
        ? {
            message: "Choose a watchlist action.",
            session_id: "abc123",
            metadata: {
              view: "pairs_picker",
              actions: ["View current pairs", "Add a pair", "Remove a pair"],
              pairs: ["XAUUSD", "EURUSD"],
            },
          }
        : {
            message: "Added USDCAD to your watchlist.",
            session_id: "abc123",
            metadata: { pairs: ["XAUUSD", "EURUSD", "USDCAD"] },
          },
    );
  };

  const runPromise = runCliForTest({
    argv: [],
    console: fakeConsole,
    fetch,
    stdin,
    stdout,
    prompts,
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/pairs\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 2);
  assert.equal(JSON.parse(calls[0].options.body).message, "/pairs");
  assert.equal(JSON.parse(calls[1].options.body).message, "/pairs add USDCAD");
  assert.deepEqual(JSON.parse(calls[1].options.body).history, [{ role: "user", content: "/pairs add USDCAD", metadata: {} }]);
});

test("runCli falls back to backend rendering for /model without tty support", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: false });
  const fetch = async () => createJsonResponse({
    message: "Choose the active model for this session.",
    session_id: "abc123",
    metadata: {
      view: "model_picker",
      current: "auto",
      options: [["auto", "Gemini -> OpenAI fallback", "Best default"]],
    },
  });

  const exitCode = await runCliForTest({
    argv: ["/model"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.match(fakeConsole.messages[2], /Choose the active model/);
});

test("runCli uses a selector for /calendar in tty mode", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  const stdin = new PassThrough();
  stdin.isTTY = true;
  const prompts = {
    async select() {
      return { view: "today" };
    },
  };
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === `${BACKEND_BASE_URL}/calendar?view=today`) {
        return createJsonResponse({
          view: "today",
          provider: "twelvedata",
          events: [{ date: "2026-03-09", time_utc: "13:30", currency: "USD", impact: "High", event_name: "CPI" }],
          warnings: [{ message: "USD CPI affects XAUUSD." }],
        });
    }
    if (url === `${BACKEND_BASE_URL}/chat`) {
      return createJsonResponse({
        message: "Calendar reviewed, EURUSD stays constructive.",
        session_id: "abc123",
        metadata: {},
      });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const runPromise = runCliForTest({
    argv: [],
    console: fakeConsole,
    fetch,
    stdin,
    stdout,
    prompts,
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/calendar\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("What now?\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.ok(fakeConsole.messages.some(message => /USD \| High \| CPI/.test(message)));
  assert.equal(calls[calls.length - 1].url, `${BACKEND_BASE_URL}/chat`);
});

test("runCli keeps the chat loop responsive after repeated selector commands", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  stdout.columns = 48;
  const stdin = new PassThrough();
  stdin.isTTY = true;
  const calls = [];
  let modelSelections = 0;
  let calendarSelections = 0;
  const prompts = {
    async select(options, context) {
      assert.equal(context.input, stdin);
      assert.equal(context.output, stdout);
      assert.equal(context.clearPromptOnDone, true);
      assert.ok(context.signal);
      if (options.message === "Select AI model") {
        modelSelections += 1;
        return "openai";
      }
      if (options.message === "Calendar view") {
        calendarSelections += 1;
        return { view: "today" };
      }
      throw new Error(`Unexpected selector: ${options.message}`);
    },
  };
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === `${BACKEND_BASE_URL}/calendar?view=today`) {
      return createJsonResponse({ view: "today", provider: "twelvedata", events: [], warnings: [] });
    }
    if (url === `${BACKEND_BASE_URL}/chat`) {
      const message = JSON.parse(options.body).message;
      if (message === "/model") {
        return createJsonResponse({
          message: "Choose the active model for this session.",
          session_id: "abc123",
          metadata: {
            view: "model_picker",
            current: "auto",
            options: [["openai", "gpt-5-mini", "Use OpenAI only"]],
          },
        });
      }
      if (message === "/model openai") {
        return createJsonResponse({
          message: "Model switched to openai for this session.",
          session_id: "abc123",
          metadata: {},
        });
      }
      return createJsonResponse({
        message: "EURUSD stays constructive above support.",
        session_id: "abc123",
        metadata: {},
      });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const runPromise = runCliForTest({
    argv: [],
    console: fakeConsole,
    fetch,
    stdin,
    stdout,
    prompts,
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/model\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/calendar\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/model\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/calendar\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/model\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("What is the current trend of EURUSD?\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.equal(modelSelections, 3);
  assert.equal(calendarSelections, 2);
  assert.equal(calls.filter(call => call.url === `${BACKEND_BASE_URL}/chat`).length, 7);
  assert.match(fakeConsole.messages.at(-1), /EURUSD stays constructive/);
});

test("runCli does not abort selector signals after a successful selection", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  const stdin = new PassThrough();
  stdin.isTTY = true;
  let selectorSignal = null;
  const prompts = {
    async select(options, context) {
      selectorSignal = context.signal;
      return "openai";
    },
  };
  const fetch = async (url, options = {}) => {
    const message = JSON.parse(options.body).message;
    return createJsonResponse(
      message === "/model"
        ? {
            message: "Choose the active model for this session.",
            session_id: "abc123",
            metadata: {
              view: "model_picker",
              current: "auto",
              options: [["openai", "gpt-5-mini", "Use OpenAI only"]],
            },
          }
        : {
            message: "Model switched to openai for this session.",
            session_id: "abc123",
            metadata: {},
          },
    );
  };

  const runPromise = runCliForTest({
    argv: [],
    console: fakeConsole,
    fetch,
    stdin,
    stdout,
    prompts,
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.write("/model\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.ok(selectorSignal);
  assert.equal(selectorSignal.aborted, false);
});

test("runCli sends accumulated history on follow-up chat messages", async () => {
  const fakeConsole = createConsole();
  const stdout = new PassThrough();
  stdout.isTTY = true;
  const stdin = new PassThrough();
  stdin.isTTY = true;
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    const body = JSON.parse(options.body);
    return createJsonResponse({
      message: body.message === "What is the current trend of EURUSD?"
        ? "EURUSD is bullish."
        : "EURUSD is still bullish.",
      session_id: "abc123",
      metadata: {},
    });
  };

  const runPromise = runCliForTest({
    argv: [],
    console: fakeConsole,
    fetch,
    stdin,
    stdout,
  });

  await new Promise(resolve => setImmediate(resolve));
  stdin.write("What is the current trend of EURUSD?\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.write("Can I enter long for this trend?\n");
  await new Promise(resolve => setImmediate(resolve));
  stdin.end("quit\n");

  const exitCode = await runPromise;

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 2);
  assert.deepEqual(JSON.parse(calls[1].options.body).history, [
    { role: "user", content: "What is the current trend of EURUSD?", metadata: {} },
    { role: "assistant", content: "EURUSD is bullish.", metadata: {} },
    { role: "user", content: "Can I enter long for this trend?", metadata: {} },
  ]);
});

test("renderChatResponse prints markdown without raw markers", () => {
  const fakeConsole = createConsole();

  renderChatResponse(fakeConsole, {
    message: "### **Market Context**\n* **Bias:** Bullish\n* **Session:** Asia open",
    metadata: {},
  }, { styled: false });

  assert.match(fakeConsole.messages[0], /Market Context/);
  assert.match(fakeConsole.messages[0], /• Bias: Bullish/);
  assert.doesNotMatch(fakeConsole.messages[0], /\*\*|###/);
});

test("renderChatResponse wraps long responses to the terminal width", () => {
  const fakeConsole = createConsole();

  renderChatResponse(fakeConsole, {
    message: "Prophet keeps the response readable even when the market explanation is long and detailed.",
    metadata: {},
  }, {
    styled: false,
    output: { columns: 34 },
  });

  const lines = fakeConsole.messages[0].trim().split("\n");
  assert.ok(lines.every(line => line.length <= 34));
});

test("renderChatResponse prints the help command list", () => {
  const fakeConsole = createConsole();

  renderChatResponse(fakeConsole, {
    message: "Open the command palette below.",
    metadata: {
      view: "help_menu",
      commands: [
        ["/help", "Show the command palette"],
        ["/model", "Inspect or switch the active session model"],
      ],
    },
  }, { styled: false });

  assert.match(fakeConsole.messages[0], /Open the command palette below/);
  assert.equal(fakeConsole.messages[1], "  /help         Show the command palette");
  assert.equal(fakeConsole.messages[2], "  /model        Inspect or switch the active session model");
});

test("renderChatResponse aligns wrapped help descriptions under the description column", () => {
  const fakeConsole = createConsole();

  renderChatResponse(fakeConsole, {
    message: "Open the command palette below.",
    metadata: {
      view: "help_menu",
      commands: [
        ["/calendar", "Inspect this week's calendar drivers and event risks for the current watchlist"],
      ],
    },
  }, {
    styled: false,
    output: { columns: 42 },
  });

  const wrappedLines = fakeConsole.messages[1].split("\n");
  assert.equal(wrappedLines[0], "  /calendar     Inspect this week's");
  assert.equal(wrappedLines[1], "                calendar drivers and event");
});

test("renderChatResponse prints model picker details", () => {
  const fakeConsole = createConsole();

  renderChatResponse(fakeConsole, {
    message: "Choose the active model for this session.",
    metadata: {
      view: "model_picker",
      current: "auto",
      options: [
        ["auto", "Gemini -> OpenAI fallback", "Best default for most sessions"],
        ["gemini", "gemini-3-flash-preview", "Fast market reasoning with Gemini only"],
      ],
    },
  }, { styled: false });

  assert.match(fakeConsole.messages[0], /Choose the active model for this session/);
  assert.equal(fakeConsole.messages[1], "Current model: auto");
  assert.equal(fakeConsole.messages[2], "  auto     Gemini -> OpenAI fallback");
  assert.equal(fakeConsole.messages[3], "           Best default for most sessions");
});

test("runCli drives the styled spinner for tty chat requests", async () => {
  const fakeConsole = createConsole();
  const stream = createStream({ isTTY: true });
  const { fetch } = createFetch({
    body: JSON.stringify({ message: "Hello from Prophet", session_id: "abc123" }),
  });

  await runCliForTest({
    argv: ["Search the web for Gold headlines"],
    console: fakeConsole,
    fetch,
    stdin: process.stdin,
    stdout: stream,
    randomFn: () => 0,
  });

  const webLabels = loadingLabelsFor("/chat", { message: "Search the web for Gold headlines" }, { randomFn: () => 0 });
  assert.ok(stream.writes.some(chunk => webLabels.some(label => chunk.includes(label))));
  assert.ok(stream.writes.some(chunk => chunk.includes("\u001b[")));
  assert.ok(stream.writes.some(chunk => /\r\s+\r/.test(chunk)));
});

test("runCli attaches X-Device-Token from saved config", async () => {
  const config = createConfigStub({ device_token: "device-123", display_name: "Tafar", onboarded: true });
  const { calls, fetch } = createFetch({
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    body: JSON.stringify({ message: "Hello from Prophet", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: createConsole(),
    fetch,
    config,
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(calls[0].options.headers["X-Device-Token"], "device-123");
});

test("runCli triggers onboarding on first run and uses the saved device token", async () => {
  const config = createConfigStub(null);
  let onboardingCalls = 0;
  const { calls, fetch } = createFetch({
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    body: JSON.stringify({ message: "Hello from Prophet", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: createConsole(),
    fetch,
    config,
    enableProfileBootstrap: true,
    runOnboarding: async ({ config: onboardingConfig }) => {
      onboardingCalls += 1;
      onboardingConfig.writeConfig({
        device_token: "fresh-device",
        display_name: "Tafar",
        created_at: "2026-03-11T00:00:00.000Z",
        onboarded: true,
      });
      return { status: "completed" };
    },
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(onboardingCalls, 1);
  assert.equal(calls[0].options.headers["X-Device-Token"], "fresh-device");
});

test("runCli clears incomplete config and reruns onboarding", async () => {
  const config = createConfigStub({ display_name: "Tafar", onboarded: true });
  const fakeConsole = createConsole();
  let onboardingCalls = 0;
  const { calls, fetch } = createFetch({
    headers: {
      get(name) {
        return name.toLowerCase() === "content-type" ? "application/json" : "";
      },
    },
    body: JSON.stringify({ message: "Hello from Prophet", session_id: "abc123", metadata: {} }),
  });

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    config,
    enableProfileBootstrap: true,
    runOnboarding: async ({ config: onboardingConfig }) => {
      onboardingCalls += 1;
      onboardingConfig.writeConfig({
        device_token: "fresh-device",
        display_name: "Tafar",
        created_at: "2026-03-11T00:00:00.000Z",
        onboarded: true,
      });
      return { status: "completed" };
    },
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(config.clearCalls, 1);
  assert.equal(onboardingCalls, 1);
  assert.ok(fakeConsole.messages.some(message => /Profile config appears incomplete/i.test(message)));
  assert.equal(calls[0].options.headers["X-Device-Token"], "fresh-device");
});

test("runCli clears stale config on profile 404 and reruns onboarding", async () => {
  const config = createConfigStub({ device_token: "stale-device", display_name: "Old", onboarded: true });
  const fakeConsole = createConsole();
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === `${BACKEND_BASE_URL}/api/v1/profile`) {
      return {
        ok: false,
        status: 404,
        headers: { get() { return "application/json"; } },
        async text() {
          return JSON.stringify({ detail: "Profile not found" });
        },
      };
    }
    return createJsonResponse({ message: "Hello from Prophet", session_id: "abc123", metadata: {} });
  };

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    config,
    enableProfileBootstrap: true,
    runOnboarding: async ({ config: onboardingConfig }) => {
      onboardingConfig.writeConfig({
        device_token: "fresh-device",
        display_name: "Tafar",
        created_at: "2026-03-11T00:00:00.000Z",
        onboarded: true,
      });
      return { status: "completed" };
    },
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(config.clearCalls, 1);
  assert.equal(calls.at(-1).options.headers["X-Device-Token"], "fresh-device");
});

test("runCli clears backend-rejected config on profile 400 and reruns onboarding", async () => {
  const config = createConfigStub({ device_token: "bad-device", display_name: "Old", onboarded: true });
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === `${BACKEND_BASE_URL}/api/v1/profile`) {
      return {
        ok: false,
        status: 400,
        headers: { get() { return "application/json"; } },
        async text() {
          return JSON.stringify({ detail: "Invalid device token" });
        },
      };
    }
    return createJsonResponse({ message: "Hello from Prophet", session_id: "abc123", metadata: {} });
  };

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: createConsole(),
    fetch,
    config,
    enableProfileBootstrap: true,
    runOnboarding: async ({ config: onboardingConfig }) => {
      onboardingConfig.writeConfig({
        device_token: "fresh-device",
        display_name: "Tafar",
        created_at: "2026-03-12T00:00:00.000Z",
        onboarded: true,
      });
      return { status: "completed" };
    },
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(config.clearCalls, 1);
  assert.equal(calls.at(-1).options.headers["X-Device-Token"], "fresh-device");
});

test("runCli continues silently when profile lookup fails offline", async () => {
  const config = createConfigStub({ device_token: "device-123", display_name: "Tafar", onboarded: true });
  const fakeConsole = createConsole();
  let profileAttempted = false;
  const fetch = async (url, options = {}) => {
    if (url === `${BACKEND_BASE_URL}/api/v1/profile`) {
      profileAttempted = true;
      throw new Error("network offline");
    }
    return createJsonResponse({ message: "Hello from Prophet", session_id: "abc123", metadata: {} });
  };

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    config,
    enableProfileBootstrap: true,
    stdout: createStream(),
  });

  assert.equal(exitCode, 0);
  assert.equal(profileAttempted, true);
  assert.ok(!fakeConsole.messages.some(message => /could not verify your saved profile/i.test(message)));
});

test("runCli skips runner profile bootstrap for whatsapp mode", async () => {
  const fakeConsole = createConsole();
  let profileChecks = 0;
  let gatewayCalls = 0;
  const config = createConfigStub({ device_token: "device-123", display_name: "Tafar", onboarded: true });

  const exitCode = await runCliForTest({
    argv: ["whatsapp"],
    console: fakeConsole,
    config,
    runWhatsAppGateway: async () => {
      gatewayCalls += 1;
      return 0;
    },
    fetch: async (url) => {
      if (String(url).includes("/api/v1/profile")) {
        profileChecks += 1;
        return createJsonResponse({ display_name: "Tafar" });
      }
      throw new Error(`Unexpected URL: ${url}`);
    },
  });

  assert.equal(exitCode, 0);
  assert.equal(profileChecks, 0);
  assert.equal(gatewayCalls, 1);
});

test("runCli retries chat requests once after a network error", async () => {
  const fakeConsole = createConsole();
  const stream = createStream();
  let attempts = 0;
  const fetch = async () => {
    attempts += 1;
    if (attempts === 1) {
      throw new Error("fetch failed");
    }
    return createJsonResponse({ message: "Recovered response", session_id: "abc123", metadata: {} });
  };

  const exitCode = await runCliForTest({
    argv: ["hello there"],
    console: fakeConsole,
    fetch,
    stdout: stream,
  });

  assert.equal(exitCode, 0);
  assert.equal(attempts, 2);
  assert.match(fakeConsole.messages[2], /Recovered response/);
});

test("runCli times out chat requests after 60 seconds and retries once", async () => {
  const fakeConsole = createConsole();
  const stream = createStream();
  let attempts = 0;
  const fetch = async (url, options = {}) => {
    attempts += 1;
    return await new Promise((resolve, reject) => {
      if (options.signal && typeof options.signal.addEventListener === "function") {
        options.signal.addEventListener("abort", () => {
          reject(options.signal.reason || new Error("aborted"));
        }, { once: true });
      }
      void resolve;
    });
  };

  await assert.rejects(
    () => runCliForTest({
      argv: ["hello there"],
      console: fakeConsole,
      fetch,
      stdout: stream,
      timeoutMs: 5,
    }),
    error => {
      assert.match(String(error && error.message), /timed out/i);
      return true;
    },
  );

  assert.equal(attempts, 2);
});
