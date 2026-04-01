"use strict";

function createFallbackChalk() {
  const applyStyle = (text, { rgb, bold = false } = {}) => {
    const codes = [];
    if (bold) {
      codes.push("1");
    }
    if (Array.isArray(rgb) && rgb.length === 3) {
      codes.push(`38;2;${rgb[0]};${rgb[1]};${rgb[2]}`);
    }
    if (codes.length === 0) {
      return String(text);
    }
    return `\u001b[${codes.join(";")}m${String(text)}\u001b[39m${bold ? "\u001b[22m" : ""}`;
  };

  return {
    hex(hex) {
      const normalized = String(hex || "").replace(/^#/, "");
      const rgb = normalized.length === 6
        ? [
            Number.parseInt(normalized.slice(0, 2), 16),
            Number.parseInt(normalized.slice(2, 4), 16),
            Number.parseInt(normalized.slice(4, 6), 16),
          ]
        : null;

      const colorize = text => applyStyle(text, { rgb });
      colorize.bold = text => applyStyle(text, { rgb, bold: true });
      return colorize;
    },
  };
}

function loadChalk() {
  try {
    const chalkModule = require("chalk");
    if (chalkModule && typeof chalkModule.Instance === "function") {
      return new chalkModule.Instance({ level: 3 });
    }
    if (chalkModule && typeof chalkModule.hex === "function") {
      return chalkModule;
    }
  } catch {
    // Fall through to the local ANSI implementation when npm deps are absent.
  }
  return createFallbackChalk();
}

const chalk = loadChalk();

const makeColor = hex => text => chalk.hex(hex)(String(text));
const makeBoldColor = hex => text => chalk.hex(hex).bold(String(text));

const theme = {
  orange: makeColor("#D97757"),
  orangeBold: makeBoldColor("#D97757"),
  blue: makeColor("#6A9BCC"),
  blueBold: makeBoldColor("#6A9BCC"),
  green: makeColor("#788C5D"),
  greenBold: makeBoldColor("#788C5D"),
  white: makeColor("#FAF9F5"),
  whiteBold: makeBoldColor("#FAF9F5"),
  muted: makeColor("#B0AEA5"),
  subtle: makeColor("#E8E6DC"),
  warning: makeColor("#E8A020"),
  warningBold: makeBoldColor("#E8A020"),
  bearish: makeColor("#C0392B"),
  bearishBold: makeBoldColor("#C0392B"),
  border: makeColor("#B0AEA5"),

  toolCall: makeBoldColor("#D97757"),
  reasoning: makeColor("#6A9BCC"),
  system: makeColor("#6A9BCC"),
  response: makeColor("#FAF9F5"),
  responseBold: makeBoldColor("#FAF9F5"),
  warn: text => chalk.hex("#E8A020")(`⚠ ${String(text)}`),
  warningText: makeColor("#E8A020"),
  bull: makeBoldColor("#788C5D"),
  bear: makeBoldColor("#C0392B"),
  pair: makeBoldColor("#D97757"),
  dim: makeColor("#B0AEA5"),
};

module.exports = theme;
