# Replit AI Agent — Roblox Studio Bridge

## Overview
A web application that bridges an AI assistant (Claude) with a Roblox Studio plugin. The web UI provides a chat interface and the plugin communicates with the server over HTTP to execute tools inside Roblox Studio.

## Architecture

- **Backend**: Flask (Python) on port 5000
- **AI**: Anthropic Claude (claude-sonnet-4-20250514)
- **Frontend**: Vanilla HTML/CSS/JS served via Flask templates
- **Plugin**: Roblox Studio Luau plugin (connects via HTTP polling)

## Key Files

- `main.py` — Flask server with all API endpoints and AI logic
- `templates/index.html` — Frontend UI (dark-themed chat interface)

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the web UI |
| `/status` | GET | Returns plugin connection status |
| `/ai` | POST | Chat or Agent mode AI request |
| `/ai/approve` | POST | Approves agent plan and starts execution |
| `/plugin/heartbeat` | POST | Plugin sends heartbeat every 2s |
| `/plugin/poll` | POST | Plugin polls for tool calls |
| `/plugin/tool_result` | POST | Plugin sends back tool execution results |
| `/session/<id>` | GET | Get session state |

## Plugin Setup
In the Roblox plugin script, set `SERVER_BASE_URL` to your Replit app URL (e.g. `https://your-repl.replit.app`).

## Modes
- **Chat**: Direct conversation with Claude about your game
- **Agent**: Multi-step task execution with plan approval before running tools

## Environment Variables
- `ANTHROPIC_API_KEY` — Required for Claude AI calls
