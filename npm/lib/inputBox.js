"use strict";

const theme = require("./theme");

function getBoxWidth(stream = process.stdout) {
  const columns = stream && Number.isFinite(stream.columns) && stream.columns > 0
    ? Math.floor(stream.columns)
    : 80;
  return Math.max(12, Math.min(columns, 80));
}

function drawInputBoxTop(output = process.stdout) {
  const width = getBoxWidth(output);
  const horizontal = "─".repeat(width - 2);
  output.write(`${theme.border(`┌${horizontal}┐`)}\n`);
}

function drawInputBoxBottom(output = process.stdout) {
  const width = getBoxWidth(output);
  const horizontal = "─".repeat(width - 2);
  output.write(`${theme.border(`└${horizontal}┘`)}\n`);
}

function getPromptString() {
  return `${theme.border("│ ")}${theme.response(">")}${theme.border(" ")}`;
}

module.exports = {
  drawInputBoxBottom,
  drawInputBoxTop,
  getBoxWidth,
  getPromptString,
};
