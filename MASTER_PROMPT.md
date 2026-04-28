# RUX — MASTER PROMPT
*A complete spec of the Rux web app — every page, every button, every modal, every tool. Use this as the single source of truth when re-generating, redesigning, or rebuilding the site.*

---

## 0. Product Summary
**Rux** is a web app that connects an AI assistant to a user's **Roblox Studio** session through a Studio plugin bridge. Users sign up on the website, install the Rux plugin in Studio, link the two with a 4-character code, and then chat with one of 11 AI models that can read, write, and manage scripts and Instances inside Studio in real time.

- **Tagline:** "Build smarter in Roblox Studio"
- **Brand colors:** pure black (`#000`) background, off-white text (`#fff`), warm gold accent (`#e6be46`)
- **Typography:** Inter (UI), JetBrains Mono (code)
- **Aesthetic:** dark, minimal, high-contrast, sharp edges with occasional rounded pills, subtle grid background, faint gold radial glow, smooth fade-up reveal animations
- **Stack hint:** Flask + vanilla JS + JSON file store; multi-provider AI (Anthropic, Google Gemini, OpenAI, OpenRouter)

---

## 1. Site Map (every URL)

| Route | Page | Purpose |
|---|---|---|
| `/` | Landing | Marketing site |
| `/app` | App | Authenticated chat workspace |
| `/code/<token>` | App (auto-connect) | Same as `/app` but used after the plugin opens a deep-link with a session token |
| `/admin` | Admin Console | Operator dashboard (admin-only) |

---

## 2. LANDING PAGE — `/`
A single long-scroll marketing page with sticky nav, scroll-progress bar at the very top (gold), and an auth modal that opens over the page.

### 2.1 Top navigation (fixed)
- Left: Rux mark (white rounded square containing a black lightning bolt SVG) + "Rux" wordmark (800 weight)
- Center/right: text links — **Features · Models · How it works · Security · FAQ**
- Vertical divider
- Ghost button: **Log in** (pill, transparent border)
- Solid white pill button: **⚡ Get started** (lightning icon)
- Mobile: hamburger that animates into an X; opens a full-width slide-down menu with the same items plus a primary "Get started free" CTA
- Background blurs and gains a 1-px bottom border once the user scrolls past 30 px

### 2.2 Hero section
- Pill badge: gold dot + "Now with 11 AI models"
- Huge headline (clamp 44–80px, weight 900, letter-spacing -3px):
  **"Build *smarter*<br>in Roblox Studio"** — the word *smarter* is gold and italic
- Sub: "Connect an AI assistant to your Roblox Studio plugin. Read, write, and manage scripts and instances through conversation."
- Two CTAs:
  - Primary white pill: **⚡ Start for free** (opens auth modal in register mode)
  - Ghost pill: **See how it works** (smooth-scrolls to `#how`)
- Decorative background: subtle 64-px square grid that slowly drifts, plus a pulsing gold radial glow
- Below the CTAs: a hand-drawn **mock of the actual app** — fake browser chrome with three dots and `rux.app/app` URL, a tiny sidebar (logo, "+ New conversation", a couple of fake conversations), a chat pane showing a Chat/Agent toggle, a user message ("Read PlayerController and fix the jump bug"), an AI reply, and a tool-call card reading `read_script → PlayerController`. The whole mock has a slow floating animation.

### 2.3 Stats strip (4 columns, sharp edges)
- **11** AI Models
- **24** Studio Tools
- **10+** Free Credits (the "+" is gold)
- **1** Plugin Install

Numbers count up from 0 when scrolled into view.

### 2.4 Features section — `#features`
Eyebrow "FEATURES" (gold, uppercase) → title "Everything you need to build faster" → sub "Rux gives your AI full access to your Roblox Studio session through a secure plugin bridge."

Eight cards in a responsive grid (sharp 1-px borders, gold icon tile in upper left):

1. **Script read and write** — Read, create, edit, and delete any script with auto-snapshots before every change.
2. **Agent mode** — Multi-step tasks: AI drafts a plan you approve, then executes tool by tool.
3. **Code search** — Search every script for variables, functions, or patterns.
4. **Multi-model AI** — Switch between Gemini, Claude, GPT, Qwen3, and more per conversation. Free models available.
5. **Saved conversations** — Auto-saved, resumable from the sidebar.
6. **Checkpoint and restore** — Every agent run creates a checkpoint; restore any one from the web UI.
7. **Instance explorer** — Browse the game hierarchy, get/set properties, find instances by name.
8. **Error diagnostics** — Check scripts for syntax issues and view output / error logs.

### 2.5 Models section — `#models`
Eyebrow "AI MODELS" → title "Pick the right brain for the job" → sub "Switch models per conversation — from free open-source to the most powerful frontier models."

Grid of 11 rounded model cards, each showing **name + colored badge + provider + one-line description**:

| Model | Provider | Badge | Description |
|---|---|---|---|
| Gemini Flash | Google | Fast (blue) | Ultra-fast for quick questions |
| Gemini Pro | Google | Smart (purple) | Deep reasoning for complex logic |
| Claude Sonnet | Anthropic | Balanced (green) | Best balance of speed and tool use |
| Claude Opus | Anthropic | Powerful (gold) | Maximum intelligence for agent tasks |
| GPT-5.3 Chat | OpenAI | Smart | General purpose with strong code |
| GPT-5.3 Codex | OpenAI | Code (orange) | Specialized for code generation |
| Qwen3 Coder | OpenRouter | Smart | Open-source with Luau strength |
| Grok 4 | OpenRouter | Smart | Advanced analytical reasoning |
| GLM-5.1 | OpenRouter | Powerful | Multilingual with coding ability |
| Gemma 4 31B | OpenRouter | Free (faint green) | No credits consumed |
| Gemma 4 26B | OpenRouter | Free | Lightweight, zero cost |

### 2.6 How it works — `#how`
4-step vertical timeline (numbered circles with a connecting line):
1. **Install the Studio plugin** — from the Roblox Creator Store.
2. **Get your connection code** — click "Connect Studio" in the web sidebar; copy the 4-char code (e.g. `A7K3` shown in a code block).
3. **No API keys needed** — Rux handles all AI integrations server-side.
4. **Start chatting** — choose Chat (quick) or Agent (multi-step).

On hover, the number circle turns gold.

### 2.7 Security — `#security`
Three rounded cards with gold lock/shield icons:
- **Encrypted passwords** — bcrypt; no plaintext stored or transmitted.
- **Server-side AI keys** — Keys never reach the browser or Studio.
- **Auto-checkpoints** — Every agent task saves script state before modifications.

### 2.8 FAQ — `#faq`
Accordion (sharp-edged) — clicking rotates the chevron and turns the open question gold:
1. Is Rux free to use?
2. Does Rux modify my game without permission?
3. Which model should I use?
4. How does the Studio plugin connect?
5. Can I undo changes?
6. Is my code sent to AI providers?

### 2.9 Final CTA
"Ready to build smarter?" + "Create a free account and start your first conversation in seconds." + the white **⚡ Get started free** pill.

### 2.10 Footer
Brand mark + "Rux" · text links (Features / Models / How it works / FAQ) · copy text "Roblox Studio AI Bridge".

### 2.11 Auth modal (overlay)
Triggered by Log in or Get started. Centered card on a blurred black overlay.
- Header: small Rux mark + "Rux" wordmark
- Tab switcher pill: **Log in / Sign up** (active = white pill)
- Inputs: Email · Password (Sign-up adds password-strength bar with weak/medium/strong colors and a hint string)
- White submit pill: "Log in" / "Create account"
- Inline red error text
- Top-right close (×)

---

## 3. APP PAGE — `/app` (authenticated)

A two-pane chat workspace. Left = sidebar, right = chat. Persistent top announcement bar (admin-controlled) sits above the chat pane.

### 3.1 Sidebar (240 px)
- Brand row: Rux mark + "Rux" + a tiny logout icon-button (top right)
- **+ New conversation** button (white pill, disabled until plugin connected; Ctrl+N also works)
- Search input ("Search...") that filters conversations live
- Conversation list: each row shows a small dot (gold = active) + title; right-click / hover shows actions (rename, delete)
- Empty state copy: "No conversations yet"
- Bottom block:
  - **Settings** button (opens settings modal)
  - **Plugin status row** — colored dot + "Studio connected" / "Disconnected"
  - **Disconnect** button (only when connected) — unlinks the plugin

Mobile: hamburger button collapses/expands the sidebar.

### 3.2 Top bar of the chat pane
- **Connection bar** (slim) — red dot + "Studio disconnected" with a **Connect** button on the right; turns green & hides the button when connected.
- **Announcement bar** (renders only if active announcements exist; admin-controlled, dismissable).
- **Agent progress bar** — thin gold bar that animates while an agent is running.
- **Agent status line** — live "Running / Executing / Restoring / Error" + current step + token count.

### 3.3 Empty state ("Dashboard")
Shown for new conversations:
- Greeting headline
- Four suggestion cards that pre-fill the input on click:
  - "Read all my scripts"
  - "Fix the bug in my script"
  - "Explain the game instance tree"
  - "Add a health system to my game"
- Inline "Studio disconnected/connected" pill

### 3.4 Plan banner (Agent mode only)
When the AI proposes a plan, a banner appears with the plan text and two buttons:
- **✓ Approve** — runs the plan
- **Discard** — drops the plan

### 3.5 Messages area
- User bubbles (right, white background, black text)
- Assistant bubbles (left, dark surface, light text)
- Tool-call cards: pill-style row with icon + monospace `tool_name → arg` text
- "Thinking…" indicator while the AI is generating
- Inline log lines for system events: ✅ result, ❌ error, ⚠️ warn, 🔗 system, with optional sub-text

### 3.6 Input area (bottom)
A composer card with three trigger pills above the textarea:
- **Model pill** (left) — opens the **Model picker** dropdown
- **Credits pill** (center) — shows remaining credits (e.g. `10.00`); read-only
- **Mode pill** — opens the **Mode picker**
- **Checkpoint pill** — opens the **Checkpoint history panel**; shows a count badge

Below: textarea (`Ask about your Roblox game...`), white **Send** button (paper-plane), and a **Stop** button (square) shown while a request is in flight.

When credits hit 0: input is replaced by a "no credits" panel with a regen countdown timer (credits regenerate every 6 hours).

#### Model picker
Top tabs: **All / Google / Anthropic / OpenAI / OpenRouter**. Below: clickable rows with provider icon, name, short description, and a colored badge (Default / Fast / Smart / Power / Code / Free). Selecting persists as default model in user preferences.

#### Mode picker
Two rows:
- **Chat** — Quick questions (default)
- **Agent** — Multi-step tasks (requires Claude models)

#### Checkpoint panel
Header "History (n)" + close × · scrollable list of checkpoints, each with:
- Status dot (gold = has scripts)
- Label + relative timestamp + script count
- Up to 3 script-name chips, "+N" if more
- **Restore** button (disabled if Studio not connected) — confirms then writes the saved scripts back to Studio
- **✕** delete

### 3.7 Connect Studio modal
Opens from "Connect" buttons. Shows:
- Title: "Connect Roblox Studio"
- Sub: "Enter this code in the Rux plugin inside Studio."
- Big monospace 4-character code (e.g. `A7K3`) + small **Copy** button
- Status: "Waiting for plugin..." (pulses)
- Tip: "Roblox ID detected — may auto-connect" or "Add your Roblox ID for auto-connect."
- **New code** refresh button
- Auto-closes the moment the plugin connects

### 3.8 Settings modal
Opens from sidebar "Settings".
- Header shows the user's email
- **Change password**: Current password + New password (min 6 chars) + "Change Password" button + inline error
- Divider
- **Roblox User ID** input (with helper "Find at roblox.com → profile → number in URL") + "Save Roblox ID" button — enables auto-connect
- Divider
- **Delete Account** (red ghost) — double confirm + password prompt; wipes user and signs out

### 3.9 Toasts & keyboard
- Bottom-center toasts for success / error / default
- Esc closes any open modal/panel
- Ctrl+N starts a new conversation

---

## 4. ADMIN CONSOLE — `/admin`

Login screen first, then a tabbed dashboard. Same dark aesthetic. Topbar shows the brand and a **Maintenance ON/OFF** toggle that, when on, blocks non-admin login site-wide.

### 4.1 Tabs (keyboard d/u/a/s/t)

#### Dashboard
- Stat cards: **Users · Conversations · Active sessions · (other metrics)**
- "X new today" sub-counts
- 7-day signup mini-chart (built from user `created_at`)

#### Users
- Search by email/ID
- Filter by **role** (user/admin) and **status** (active/blocked)
- Multi-select checkbox column with bulk actions:
  - **Block / Unblock**
  - **Delete**
  - **Grant credits** (modal)
- Per-row actions:
  - Open detail drawer (profile, credits, last 15 credit history, conversations)
  - Edit credits (modal)
  - Block / Unblock
  - Promote / Demote admin
  - Delete
  - Add note
- Pagination at bottom

#### Activity
- **Audit log** with filter dropdown (All / Login / Block / Unblock / Promote / Demote / Maintenance / etc.)
- Each row: timestamp, actor, action, target, details

#### System
Stacked cards:
- **System Configuration** — editable global config (signup enabled, default credits, regen interval, etc.)
- **Announcements** — list + create modal (title, body, severity, expiration)
- **Email / Domain Bans** — list + create modal (email or `@domain.com`)
- **Invite Codes** — generate / list / revoke (modal)
- **Webhooks** — paste webhook URLs + Save + **Test** button
- **Data Backup & Migration** — **Export all** (download JSON) and **Import** (upload JSON)

#### Tools
- **Active Sessions** — list of web sessions with revoke per-token and revoke-all-for-user
- **Connected Plugins** — live list of Studio plugins currently heartbeating
- **All Checkpoints** — every checkpoint across all users
- **Conversations** — search box, list; click opens the **Conversation Viewer modal** (full transcript) with a flag/unflag toggle
- **Global Credit History** — every credit grant/spend across all users

### 4.2 Modals (admin)
Edit credits · Grant credits · Bulk grant credits · Delete confirm · Announcement composer · Email-ban composer · Invite composer · Conversation viewer.

---

## 5. STUDIO TOOLS (the AI's full toolbox)
The AI can call these inside Studio through the plugin bridge. Server-side tools (`create_checkpoint`, `list_checkpoints`, `restore_checkpoint`) run on the server, the rest run in the plugin.

### Scripts
- `read_script(name)` — Read a script's source.
- `write_script(name, code)` — Overwrite a script's source.
- `create_script(name, type, parent)` — Create Script / LocalScript / ModuleScript under a parent path.
- `delete_script(name)` — Delete a script.
- `list_scripts()` — List every script name.
- `get_script_tree()` — JSON tree of all scripts and their paths.
- `check_errors(name)` — Detect syntax / script issues.
- `search_code(query)` — Search every script for a string.
- `find_usages(variable_name)` — Find usages of a variable or function name.

### Logs
- `get_output_log()` — Recent output log lines.
- `get_error_log()` — Recent error log lines.

### Instance / Explorer
- `get_instance_tree()` — Full Explorer tree (large).
- `get_properties(instance_path)` — All readable properties.
- `set_property(instance_path, property, value)` — Mutate any writable property (Position, Size, Color, CanCollide, Anchored, Material, Parent, CFrame, …).
- `add_instance(class_name, parent_path, name, properties?)` — Create any Roblox Instance (Folder, Part, Tool, MeshPart, PointLight, Sound, Decal, BillboardGui, Attachment, etc.).
- `find_instance(name)` — Find an instance by name anywhere.
- `get_selection()` — Current Explorer selection.
- `get_current_script()` — Currently active script + source.
- `get_place_metadata()` — Game name, place ID, version.

### Snapshots & checkpoints
- `snapshot_script(name)` — Save a snapshot before modification.
- `diff_script(name)` — Diff against last snapshot.
- `restore_script(name)` — Roll back a single script.
- `create_checkpoint(label, scripts)` — Save a labeled checkpoint of one or more scripts (server-side).
- `list_checkpoints()` — List checkpoints for the session (server-side).
- `restore_checkpoint(checkpoint_id)` — Restore saved scripts from a checkpoint (server-side).

---

## 6. AUTH, CREDITS & STATE

- Email + password sign-up; passwords hashed with werkzeug bcrypt.
- Session is a token kept in `localStorage`; sent as `Authorization` header.
- Optional **Roblox User ID** on profile → enables auto-connect when the plugin starts.
- **Credits**: every user starts with **10**, regenerate every **6 hours**. Free models (Gemma 4 31B / 26B) consume 0. Heavy models (Opus, GPT-5.3) deduct per token.
- **User preferences** persist `default_model` and `default_mode`.

---

## 7. PLUGIN BRIDGE FLOW
1. User clicks **Connect** → server returns a 4-char code + session_id.
2. User pastes the code in the Studio plugin.
3. Plugin calls `/plugin/connect` with the code → linked to the user.
4. Plugin polls `/plugin/poll` for queued tool calls; returns results to `/plugin/tool_result`.
5. Plugin posts `/plugin/heartbeat` every few seconds; the web UI shows live status.
6. If the user has a Roblox ID set, future Studio sessions auto-link without a code.

---

## 8. VISUAL / DESIGN TOKENS (recreate the look)

```
--bg          #000
--surface     #080808
--surface2    #0e0e0e
--surface3    #141414
--border      #161616
--border2     #222
--border3     #2a2a2a
--text        #fff
--text2       #999
--text3       #666
--muted       #444
--accent      #e6be46   (gold)
--accent2     rgba(230,190,70,.08)
--danger      #e04e52
```

- Fonts: `Inter 400/500/600/700/800/900`, `JetBrains Mono 400/500`
- Buttons: white solid pill (primary), bordered ghost pill (secondary), red ghost (danger)
- Cards: 1-px borders, mostly sharp corners except model/security cards (10-px radius) and modals (16-px radius)
- Animations: fade-up on scroll, count-up stats, floating preview mock, pulsing gold radial glow, gold scroll-progress bar across the top

---

## 9. ONE-LINE PROMPT (drop-in for an AI image / site generator)

> Build a dark, minimal SaaS landing page and authenticated chat workspace for **Rux**, a tool that connects an AI assistant to Roblox Studio via a plugin. Pure-black background, white text, warm-gold accent (#e6be46), Inter typography. Landing page has a fixed glass nav (Rux mark + Features/Models/How-it-works/Security/FAQ + Log in / ⚡ Get started), a hero with a huge "Build *smarter* in Roblox Studio" headline (the word "smarter" gold and italic), a floating mock of the app, a 4-stat strip (11 models · 24 tools · 10+ free credits · 1 plugin), an 8-card features grid, a grid of 11 model cards with colored badges (Fast/Smart/Power/Code/Balanced/Free), a 4-step "How it works" timeline, three security cards, an FAQ accordion, a final CTA, and a slim footer. The app at `/app` has a 240-px sidebar (brand, + New conversation, search, conversation list, Settings, plugin-status dot, Disconnect) and a chat pane with a connection bar, announcement bar, agent-progress bar, message stream with tool-call pill cards, and a composer with model/mode/credits/checkpoint pills, textarea, send & stop buttons. Includes a Connect Studio modal that displays a 4-char code, a Settings modal (change password, Roblox ID, delete account), and a Checkpoint history panel. Admin console at `/admin` has tabs Dashboard / Users / Activity / System / Tools, plus a Maintenance toggle. The AI can call ~24 Studio tools to read/write scripts, edit Instances, search code, snapshot, and checkpoint.
