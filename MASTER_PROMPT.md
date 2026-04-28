# RUX — Master Prompt

## What it is
**Rux** is a web app that connects an AI assistant to **Roblox Studio** through a Studio plugin, so users can read, write, and manage scripts and Instances in their game by chatting.

Tagline: *"Build smarter in Roblox Studio."*

---

## Theme
- **Dark, minimal, high-contrast.** Pure black background, off-white text, warm gold accent (`#e6be46`).
- **Fonts:** Inter (UI), JetBrains Mono (code).
- **Style:** sharp 1-px borders, white pill buttons, subtle drifting grid background, faint pulsing gold radial glow, smooth fade-up reveal animations, gold scroll-progress bar at the top.

---

## Special features
- **Multi-model AI** — switch per conversation between 11 models: Gemini Flash / Pro, Claude Sonnet / Opus, GPT-5.3 Chat / Codex, Qwen3 Coder, Grok 4, GLM-5.1, and free Gemma 4 (31B & 26B).
- **Two modes** — *Chat* for quick questions, *Agent* for multi-step tasks where the AI proposes a plan you approve before it runs.
- **Studio plugin bridge** — link the web app to Studio with a 4-character code (e.g. `A7K3`); set your Roblox User ID once and future sessions auto-connect.
- **Full Studio toolbox** — the AI can read/write/create/delete scripts, search code, find usages, check syntax errors, browse the Instance tree, get/set any property, add new Instances (Parts, Tools, Folders, Lights, Sounds…), and read output / error logs.
- **Auto-checkpoints + restore** — every agent run snapshots affected scripts; restore any past checkpoint with one click.
- **Saved conversations** — auto-saved, searchable, resumable from the sidebar.
- **Credits system** — 10 free credits that regenerate every 6 hours; Gemma models cost 0.
- **No API keys needed** — all AI providers are proxied server-side; keys never touch the browser or Studio.
- **Live status everywhere** — connection bar, agent progress bar, token counter, plugin heartbeat dot.
- **Admin console** — dashboard, user management (block/promote/grant credits), audit log, announcements, email bans, invite codes, webhooks, data export/import, live sessions & plugins, conversation viewer, global maintenance toggle.

---

## How it works
1. **Sign up** on the website (email + password).
2. **Install the Rux plugin** from the Roblox Creator Store.
3. **Click "Connect Studio"** in the sidebar — copy the 4-character code shown.
4. **Paste the code** into the plugin inside Studio. (Or set your Roblox ID once for auto-connect from then on.)
5. **Pick a model and a mode**, type a request, hit send.
6. The AI calls Studio tools through the plugin to read your scripts/Instances and apply changes — every modification is preceded by an automatic checkpoint you can restore at any time.
