"use strict";

const { startWhatsAppGateway } = require("./runtime");

async function main() {
  try {
    process.exitCode = await startWhatsAppGateway({
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
