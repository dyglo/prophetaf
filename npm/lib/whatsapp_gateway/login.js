"use strict";

const { startWhatsAppGateway } = require("./runtime");

async function runWhatsAppLogin(options = {}) {
  return startWhatsAppGateway({
    ...options,
    loginOnly: true,
  });
}

module.exports = {
  runWhatsAppLogin,
};
