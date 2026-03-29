![Prophet Terminal](public/images/tumbnail.png)

# Prophet

Personal AI Trading Assistant

## What Prophet Is

Prophet is a CLI-based personal AI hedge fund terminal that combines LangChain agents, real-time market data, and a structured trading methodology for discretionary FX decision support.

## Installation

```bash
npm install -g prophetaf
prophetaf
```

## Features

- Economic calendar with today/week views and pair-aware event warnings
- Macro news analysis via live web search when current context is required
- `PROPHET.md` personal memory (`/memory`, `/remember`, `/forget`)
- Multi-pair setup ranking to focus on the strongest current watchlist opportunity
- Session resume with recap and recent conversation history
- Live AI reasoning display during agent tool execution
- SSE streaming chat responses from backend to CLI
- Interactive selectors for models, pairs, sessions, and calendar views
- WhatsApp gateway via QR scan using `whatsapp-web.js`
- Background WhatsApp daemon mode with `--daemon`, `--status`, and `--stop`

## Trading Methodology

- Markets: XAUUSD and major FX pairs (default watchlist includes XAUUSD, EURUSD, GBPUSD, USDJPY, USDCHF)
- Top-down structure: H1 bias + M15 setup execution
- Setup confluence model:
- Fair Value Gap (FVG) detection with minimum gap threshold
- Fibonacci retracement zone checks (`0.5`, `0.618`, `0.705`, `0.786`)
- Liquidity sweep detection during active sessions
- Score-based setup surfacing and ranking to prioritize high-quality opportunities

## Slash Commands (v4)

| Command | Description |
| --- | --- |
| `/help` | List all available commands |
| `/memory` | Show current PROPHET.md contents |
| `/remember [rule]` | Add a rule to PROPHET.md |
| `/forget [rule]` | Remove a rule from PROPHET.md |
| `/model` | Select the active AI model for this session |
| `/pairs` | View, add, or remove watchlist pairs |
| `/sessions` | List and resume saved sessions |
| `/calendar` | View today or this week’s calendar |
| `/exit` | End the current session |

## Architecture Overview

`npm CLI client` -> `Cloud Run FastAPI backend` -> `LangChain agent` -> `OANDA/AlphaVantage/Finnhub data` -> `Gemini/OpenAI`

## WhatsApp Gateway

Prophet can run as a separate WhatsApp gateway process so you can message yourself on WhatsApp and receive Prophet replies back in the same chat.

### Install

```bash
cd npm
npm install
```

The gateway uses `whatsapp-web.js`, which runs through WhatsApp Web and does not require the WhatsApp Business API, a Meta developer account, or API keys.

### First-Time Setup

```bash
prophetaf whatsapp
```

On the first run, Prophet will:

1. Reuse or create your Prophet profile in `~/.prophet/config.json`
2. Display a WhatsApp QR code in the terminal
3. Save the linked WhatsApp auth session locally under `~/.prophet/whatsapp-session/`

You can also run the dedicated login bootstrap directly:

```bash
cd npm
npm run gateway:login
```

### Daily Use

```bash
prophetaf whatsapp
```

Or from the npm package directory:

```bash
cd npm
npm run gateway
```

After the phone is linked, send a message to your own WhatsApp number. Prophet will intercept messages from that self-chat, send them to the hosted `/chat` backend, and reply back in plain text.

The gateway keeps a dedicated local WhatsApp chat session id in `~/.prophet/whatsapp-chat.json`, so follow-up messages preserve Prophet conversation context just like the CLI. Existing slash commands such as `/memory`, `/calendar`, `/pairs`, `/sessions`, and `/help` also work through WhatsApp.

### Daemon Mode

If you want Prophet to stay online after closing the terminal, run the WhatsApp gateway in daemon mode:

```bash
prophetaf whatsapp --daemon
```

This starts a detached background process and stores:

- daemon PID in `~/.prophet/whatsapp-daemon.pid`
- daemon state in `~/.prophet/whatsapp-daemon.json`
- daemon logs in `~/.prophet/whatsapp-daemon.log`

Check whether the daemon is running:

```bash
prophetaf whatsapp --status
```

Stop the daemon cleanly:

```bash
prophetaf whatsapp --stop
```

Foreground mode and daemon mode share the same local WhatsApp auth session under `~/.prophet/whatsapp-session/`, so you do not need to rescan the QR code when switching between them unless the WhatsApp session itself expires.

### Linux Dependencies

On Linux, `whatsapp-web.js` runs Chromium through Puppeteer. If required shared libraries are missing, install them before starting the gateway:

```bash
sudo apt-get install -y libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2
```

On Ubuntu 24.04 and newer, some package names may appear as `t64` variants such as `libatk1.0-0t64`, `libatk-bridge2.0-0t64`, `libcups2t64`, and `libasound2t64`.

## Contributing

This project is actively evolving. Contributions and practical feedback are welcome.

## License

MIT License. See [LICENSE](LICENSE).
