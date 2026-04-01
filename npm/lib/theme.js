"use strict";

const chalkModule = require("chalk");
const chalk = new chalkModule.Instance({ level: 3 });

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
