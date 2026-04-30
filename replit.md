# Rux — Roblox Studio AI Bridge

## Overview
A web application that bridges an AI assistant (Claude / Gemini) with a Roblox Studio plugin. The web UI provides a chat interface and the plugin communicates with the server over HTTP to execute tools inside Roblox Studio.

## Architecture

- **Backend**: Flask (Python) served via Gunicorn on port 5000
- **AI**: Anthropic Claude, Google Gemini, OpenAI, and OpenRouter — all wired via the Replit AI Integrations blueprints (no user-managed keys). Env vars `AI_INTEGRATIONS_*_BASE_URL` / `AI_INTEGRATIONS_*_API_KEY` are auto-set by the platform.
- **Frontend**: Vanilla HTML/CSS/JS served via Flask templates
- **Data store**: File-based JSON persistence via `store.py` in the `data/` directory
- **Plugin**: Roblox Studio Luau plugin (connects via HTTP polling)

## Key Files

- `main.py` — Flask server with all API endpoints, AI logic, tool bridge, and workspace routes
- `store.py` — File-based persistence (users, sessions, credits, conversations, workspace script cache)
- `templates/landing.html` — Landing page
- `templates/index.html` — Main chat/agent UI + Workspace (Monaco editor, script tree, AI chat)
- `templates/admin.html` — Admin dashboard
- `data/` — JSON data files (users, sessions, credits, conversations)
- `data/ws_scripts/` — Per-user offline workspace script cache
- `attached_assets/` — Roblox Luau plugin code snippets

## Workspace Feature

The Workspace is a VS Code-style IDE overlay (FAB button → full screen):
- **Left panel**: AI chat assistant scoped to the open script
- **Middle panel**: Monaco Editor with Lua syntax highlighting, AI Tab-to-accept inline suggestions
- **Right panel**: Explorer tree of all Studio scripts grouped by service

**Offline-first sync model:**
- Pulling a script from Studio saves a base snapshot + local copy to `data/ws_scripts/{user_id}.json`
- Edits auto-save to the cache every ~1.2s (no Studio connection needed)
- The "Save to Studio" button pushes local → Studio and clears the dirty flag
- On reconnect (`wsOnReconnect`): fetches current Studio content, compares with base + local
  - Studio ahead only → update local silently
  - Local ahead only → show "Push" banner
  - Both changed → AI merge (Claude Haiku) → load merged code for review
- Orange dot badge on tree items = local changes not yet pushed
- "Push all" sync banner for batch-pushing all dirty scripts

**Backend routes:**
- `POST /workspace/call` — queue a plugin tool call
- `GET /workspace/result/<req_id>` — poll result
- `POST /workspace/complete` — AI inline completion (Haiku)
- `GET /workspace/scripts/cached` — list all cached scripts
- `GET /workspace/script/content?name=...` — get cached script
- `POST /workspace/script/save` — save local edit to cache
- `POST /workspace/sync` — compare studio_content with cache, AI-merge on conflict
- `POST /workspace/push` — queue write_script tool call
- `POST /workspace/push/confirm` — mark script as cleanly pushed

## Running

The app is served via Gunicorn:
```
gunicorn --bind 0.0.0.0:5000 --reuse-port --reload main:app
```

## Checkpoint & Rollback System

Scripts can be rolled back to a saved state ("checkpoint"):
- **Auto-checkpoint**: Created automatically when an agent task is approved. As the agent reads scripts via `read_script`, their original content is captured server-side and stored in the checkpoint.
- **AI-created checkpoints**: The AI can call `create_checkpoint(label, scripts)` and `list_checkpoints()` — these are server-side tools resolved in `/plugin/poll` without plugin involvement.
- **Web UI**: The "History" button in the input bar opens the checkpoint panel listing all saved checkpoints with restore/delete buttons.
- **Restore**: Sets a `restore_queue` of `write_script` calls in the session, which the plugin processes one at a time via the normal poll/tool_result cycle without AI involvement.
- **Storage**: Checkpoints stored in `data/checkpoints/{user_id}.json`.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Landing page |
| `/app` | GET | Main chat UI |
| `/status` | GET | Returns plugin connection status |
| `/models` | GET | Returns available AI models |
| `/ai` | POST | Chat or Agent mode AI request |
| `/ai/approve` | POST | Approves agent plan and starts execution |
| `/plugin/heartbeat` | POST | Plugin sends heartbeat every 2s |
| `/plugin/poll` | POST | Plugin polls for tool calls (resolves server-side tools here) |
| `/plugin/tool_result` | POST | Plugin sends back tool execution results |
| `/api/checkpoints` | GET | List user's checkpoints |
| `/api/checkpoints` | POST | Create a checkpoint |
| `/api/checkpoints/<id>` | DELETE | Delete a checkpoint |
| `/api/checkpoints/<id>/restore` | POST | Start restoring a checkpoint |

## Plugin Setup
In the Roblox plugin script, set `SERVER_BASE_URL` to your Replit app URL (e.g. `https://your-repl.replit.app`).

## Modes
- **Chat**: Direct conversation with the AI about your game
- **Agent**: Multi-step task execution with plan approval before running tools

## Concepts
- **Project** = a chat conversation (sidebar item). Persisted via `/api/conversations`.
- **Workspace** = the per-project code editor (Monaco) opened from the in-chat **Project Bar** ("Open Workspace" button). 1:1 with the project; uses the project's name automatically.
- **Project Bar**: shown above the message list when a project is open. Lets the user rename, switch between recent projects, open the workspace, or delete the project. Replaces the previous floating workspace FAB.
- The workspace AI chat shares the main chat's `mode` and `selectedModel` state and persists changes through `/api/preferences`. Pickers live in the workspace's `.ws-chat-head`.
- The last opened project id is stored in `localStorage` under `rux_last_conv` and auto-resumed on reload.

## Environment Variables
- `AI_INTEGRATIONS_ANTHROPIC_API_KEY` — Anthropic Claude API key (optional, app starts without it)
- `AI_INTEGRATIONS_ANTHROPIC_BASE_URL` — Custom Anthropic base URL (optional)
- `AI_INTEGRATIONS_GEMINI_API_KEY` — Google Gemini API key (optional, app starts without it)
- `AI_INTEGRATIONS_GEMINI_BASE_URL` — Custom Gemini base URL (optional)

## AI Models
- `gemini-flash` — Gemini 2.5 Flash (fast)
- `gemini-pro` — Gemini 2.5 Pro (smart)
- `sonnet` — Claude Sonnet 4 (balanced)
- `opus` — Claude Opus 4 (powerful)

## Dependencies
Managed via `pyproject.toml` / `uv`. Key packages: flask, gunicorn, anthropic, google-genai, google-generativeai, bcrypt, psycopg2-binary.
