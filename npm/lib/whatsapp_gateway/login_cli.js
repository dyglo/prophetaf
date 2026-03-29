"use strict";

const { runWhatsAppLogin } = require("./login");

async function main() {
  try {
    process.exitCode = await runWhatsAppLogin({
      stdin: process.stdin,
      stdout: process.stdout,
      console,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`prophetaf: ${message}`);
    process.exitCode = 1;
  }
}

main();
