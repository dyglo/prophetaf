"use strict";

const ANSI_STRIP_PATTERN = /\u001b\[[0-9;]*m/g;

function stripAnsi(text) {
  return String(text || "").replace(ANSI_STRIP_PATTERN, "");
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

function formatPairsPicker(metadata) {
  const lines = [];
  const pairs = Array.isArray(metadata && metadata.pairs) ? metadata.pairs : [];
  const actions = Array.isArray(metadata && metadata.actions) ? metadata.actions : [];
  if (pairs.length > 0) {
    lines.push(`Current pairs: ${pairs.join(", ")}`);
  }
  if (actions.length > 0) {
    lines.push("Available actions:");
    for (const action of actions) {
      lines.push(`- ${action}`);
    }
    lines.push('Use "/pairs add PAIR" or "/pairs remove PAIR".');
  }
  return lines.join("\n").trim();
}

function formatHelpMenu(metadata) {
  const commands = Array.isArray(metadata && metadata.commands) ? metadata.commands : [];
  const lines = [];
  for (const entry of commands) {
    const [command, description] = Array.isArray(entry) ? entry : [];
    if (!command || !description) {
      continue;
    }
    lines.push(`${command} - ${description}`);
  }
  return lines.join("\n").trim();
}

function formatModelPicker(metadata) {
  const lines = [];
  if (metadata && metadata.current) {
    lines.push(`Current model: ${metadata.current}`);
  }
  const options = Array.isArray(metadata && metadata.options) ? metadata.options : [];
  if (options.length > 0) {
    lines.push("Available models:");
  }
  for (const option of options) {
    const [name, detail, note] = Array.isArray(option) ? option : [];
    if (!name || !detail) {
      continue;
    }
    lines.push(`${name} - ${detail}`);
    if (note) {
      lines.push(`  ${note}`);
    }
  }
  return lines.join("\n").trim();
}

function flattenStructuredView(response) {
  const metadata = response && response.metadata ? response.metadata : {};
  const sections = [];
  const baseMessage = String(response && response.message ? response.message : "").trim();
  if (baseMessage) {
    sections.push(baseMessage);
  }

  switch (metadata.view) {
    case "help_menu": {
      const helpText = formatHelpMenu(metadata);
      if (helpText) {
        sections.push(helpText);
      }
      break;
    }
    case "model_picker": {
      const pickerText = formatModelPicker(metadata);
      if (pickerText) {
        sections.push(pickerText);
      }
      break;
    }
    case "pairs_picker": {
      const pairsText = formatPairsPicker(metadata);
      if (pairsText) {
        sections.push(pairsText);
      }
      break;
    }
    default:
      break;
  }

  return sections.join("\n\n").trim();
}

function normalizeWhitespace(text) {
  return String(text || "")
    .replace(/\r\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .split("\n")
    .map(line => line.trimEnd())
    .join("\n")
    .trim();
}

function formatWhatsAppResponse(response) {
  const flattened = flattenStructuredView(response);
  const withoutAnsi = stripAnsi(flattened);
  const withoutMarkdown = stripMarkdownSyntax(withoutAnsi);
  return normalizeWhitespace(withoutMarkdown);
}

module.exports = {
  formatWhatsAppResponse,
  stripAnsi,
  stripMarkdownSyntax,
};
