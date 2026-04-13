import os
import json
import uuid
import time
import re
import secrets
import traceback
from flask import Flask, request, jsonify, render_template
from threading import Lock

try:
    import bcrypt
    HAS_BCRYPT = True
except ImportError:
    HAS_BCRYPT = False

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from anthropic import Anthropic
from openai import OpenAI

import store

app = Flask(__name__)

try:
    anthropic_client = Anthropic(
        api_key=os.environ.get("AI_INTEGRATIONS_ANTHROPIC_API_KEY"),
        base_url=os.environ.get("AI_INTEGRATIONS_ANTHROPIC_BASE_URL"),
    )
except Exception:
    anthropic_client = None

if GEMINI_AVAILABLE and os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY"):
    try:
        gemini_client = google_genai.Client(
            api_key=os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY"),
            http_options={
                "api_version": "",
                "base_url": os.environ.get("AI_INTEGRATIONS_GEMINI_BASE_URL"),
            },
        )
    except Exception:
        gemini_client = None
else:
    gemini_client = None

try:
    openai_client = OpenAI(
        api_key=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY"),
        base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL"),
    )
except Exception:
    openai_client = None

try:
    openrouter_client = OpenAI(
        api_key=os.environ.get("AI_INTEGRATIONS_OPENROUTER_API_KEY"),
        base_url=os.environ.get("AI_INTEGRATIONS_OPENROUTER_BASE_URL"),
    )
except Exception:
    openrouter_client = None

MAX_AGENT_STEPS = 20
MAX_TOOL_RESULT_CHARS = 30000
CODE_EXPIRY_MS = 5 * 60 * 1000
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
WEB_HEARTBEAT_TIMEOUT = 90

MODELS = {
    "gemini-flash": {"id": "gemini-2.5-flash", "provider": "google", "label": "Gemini Flash", "badge": "Fast", "credit_per_token": 0.0001},
    "gemini-pro": {"id": "gemini-2.5-pro", "provider": "google", "label": "Gemini Pro", "badge": "Smart", "credit_per_token": 0.0003},
    "sonnet": {"id": "claude-sonnet-4-6", "provider": "anthropic", "label": "Claude Sonnet", "badge": "Balanced", "credit_per_token": 0.0005},
    "opus": {"id": "claude-opus-4-6", "provider": "anthropic", "label": "Claude Opus", "badge": "Powerful", "credit_per_token": 0.001},
    "gpt-5-chat": {"id": "gpt-5.3-chat", "provider": "openai", "label": "GPT-5.3 Chat", "badge": "Smart", "credit_per_token": 0.0005},
    "gpt-5-codex": {"id": "gpt-5.3-codex", "provider": "openai", "label": "GPT-5.3 Codex", "badge": "Strong", "credit_per_token": 0.0005},
    "qwen-coder": {"id": "qwen/qwen3-coder-next", "provider": "openrouter", "label": "Qwen3 Coder", "badge": "Smart", "credit_per_token": 0.0003},
    "glm-5": {"id": "z-ai/glm-5.1", "provider": "openrouter", "label": "GLM-5.1", "badge": "Powerful", "credit_per_token": 0.0007},
    "grok-4": {"id": "x-ai/grok-4.20", "provider": "openrouter", "label": "Grok 4", "badge": "Smart", "credit_per_token": 0.0004},
    "gemma-31b": {"id": "google/gemma-4-31b-it:free", "provider": "openrouter", "label": "Gemma 4 31B", "badge": "Free", "credit_per_token": 0},
    "gemma-26b": {"id": "google/gemma-4-26b-a4b-it:free", "provider": "openrouter", "label": "Gemma 4 26B", "badge": "Free", "credit_per_token": 0},
}
DEFAULT_MODEL = "gemini-pro"
OWNER_ID = "418f27dc-3ad3-48f6-b725-a00de0091926"

def get_user_from_token(token):
    if not token:
        return None
    s = store.get_session(token)
    if not s:
        return None
    return store.get_user_by_id(s["user_id"])

def require_auth(f):
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user = get_user_from_token(token)
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if user.get("blocked"):
            return jsonify({"error": "Account blocked", "blocked": True}), 403
        request.user = user
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def require_admin(f):
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user = get_user_from_token(token)
        if not user or not store.is_admin(user.get("id", "")):
            return jsonify({"error": "Forbidden"}), 403
        request.user = user
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def require_owner(f):
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user = get_user_from_token(token)
        if not user or user.get("id") != OWNER_ID:
            return jsonify({"error": "Owner only"}), 403
        request.user = user
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

@app.before_request
def check_maintenance():
    if request.path.startswith("/admin"):
        return None
    if request.path.startswith("/auth/login") or request.path == "/":
        return None
    if request.path.startswith("/static"):
        return None
    if store.is_maintenance():
        return jsonify({"error": "maintenance", "message": "Site is under maintenance. Please try again later."}), 503
        
# ═══ IN-MEMORY STATE ═══
sessions = {}
sessions_lock = Lock()
plugin_registry = {}
plugin_registry_lock = Lock()
pending_connections = {}
pending_connections_lock = Lock()
web_heartbeats = {}
web_heartbeats_lock = Lock()

# ═══ AUTH HELPERS ═══
def hash_password(password):
    if not HAS_BCRYPT:
        return password
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password, hashed):
    if not HAS_BCRYPT:
        return password == hashed
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False



# ═══ WEB HEARTBEAT HELPERS ═══
def update_web_heartbeat(user_id):
    with web_heartbeats_lock:
        web_heartbeats[user_id] = time.time()

def is_web_active(user_id):
    with web_heartbeats_lock:
        last = web_heartbeats.get(user_id, 0)
    return (time.time() - last) < WEB_HEARTBEAT_TIMEOUT

def clear_web_heartbeat(user_id):
    with web_heartbeats_lock:
        web_heartbeats.pop(user_id, None)

# ═══ CONNECTION HELPERS ═══
def generate_code():
    return ''.join(secrets.choice(CODE_CHARS) for _ in range(4))

def clean_expired_codes():
    now = int(time.time() * 1000)
    expired = [k for k, v in pending_connections.items() if now - v["created_at"] > CODE_EXPIRY_MS]
    for k in expired:
        del pending_connections[k]

# ═══ SESSION HELPERS ═══
def get_session(session_id):
    with sessions_lock:
        if session_id not in sessions:
            sessions[session_id] = {
                "conversation": [], "agent_messages": [], "pending_tool_call": None,
                "plan": None, "approved": False, "step_count": 0, "status": "idle",
                "plugin_id": None, "logs": [], "latest_reply": "",
                "latest_context": {}, "model_key": DEFAULT_MODEL,
                "user_id": None, "accumulated_cost": 0.0,
                "script_cache": {}, "current_checkpoint_id": None,
                "restore_queue": [], "restore_checkpoint_label": "",
                "_session_id": session_id,
            }
        return sessions[session_id]

def build_context(data):
    return {
        "current_script_name": data.get("current_script_name"),
        "current_script_source": data.get("current_script_source"),
        "selected_instance": data.get("selected_instance"),
    }

def build_chat_messages(session, user_message, context):
    messages = list(session["conversation"])
    ctx_msg = f"User message:\n{user_message}\n\nCurrent script name:\n{context.get('current_script_name')}\n\nSelected instance:\n{json.dumps(context.get('selected_instance'), indent=2)}"
    if messages and messages[-1].get("role") == "user" and messages[-1].get("content") == user_message:
        messages[-1] = {"role": "user", "content": ctx_msg}
    else:
        messages.append({"role": "user", "content": ctx_msg})
    return messages

def resolve_model(key):
    return MODELS.get(key, MODELS[DEFAULT_MODEL])

def content_blocks_to_dicts(blocks):
    result = []
    for b in blocks:
        if isinstance(b, dict):
            result.append(b)
            continue
        if not hasattr(b, 'type'):
            continue
        if b.type == "text":
            result.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            result.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif b.type == "thinking":
            result.append({"type": "thinking", "thinking": getattr(b, 'thinking', ''), "signature": getattr(b, 'signature', '')})
        elif b.type == "redacted_thinking":
            result.append({"type": "redacted_thinking", "data": getattr(b, 'data', '')})
    return result

def extract_tool_info(content):
    tool_calls = []
    reply_text = ""
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "tool_use":
                tool_calls.append({"id": b["id"], "name": b["name"], "arguments": b.get("input", {})})
            elif b.get("type") == "text":
                reply_text += b.get("text", "")
        elif hasattr(b, 'type'):
            if b.type == "tool_use":
                tool_calls.append({"id": b.id, "name": b.name, "arguments": b.input})
            elif b.type == "text":
                reply_text += b.text
    first_tc = tool_calls[0] if tool_calls else None
    return first_tc, tool_calls, reply_text

def build_assistant_content(content, keep_tool_id):
    result = []
    for b in content:
        if isinstance(b, dict):
            btype = b.get("type")
            if btype == "text":
                result.append(b)
            elif btype == "tool_use" and b.get("id") == keep_tool_id:
                result.append(b)
            elif btype in ("thinking", "redacted_thinking"):
                result.append(b)
        elif hasattr(b, 'type'):
            if b.type == "text":
                result.append({"type": "text", "text": b.text})
            elif b.type == "tool_use" and b.id == keep_tool_id:
                result.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif b.type == "thinking":
                result.append({"type": "thinking", "thinking": getattr(b, 'thinking', ''), "signature": getattr(b, 'signature', '')})
            elif b.type == "redacted_thinking":
                result.append({"type": "redacted_thinking", "data": getattr(b, 'data', '')})
    if not result:
        result.append({"type": "text", "text": ""})
    return result

def truncate_tool_result(data, max_chars=MAX_TOOL_RESULT_CHARS):
    s = json.dumps(data, ensure_ascii=False)
    if len(s) <= max_chars:
        return data
    truncated = s[:max_chars]
    truncated += "\n\n... [RESULT TRUNCATED - too large. Use read_script or find_instance instead.]"
    try:
        return json.loads(truncated) if truncated.startswith(('{', '[')) else {"truncated": True, "preview": truncated[:max_chars]}
    except json.JSONDecodeError:
        return {"truncated": True, "preview": s[:max_chars], "notice": "Result truncated."}

# ═══ AI HELPERS ═══
def call_anthropic(model_id, messages, max_tokens=4096, tools=None):
    kw = dict(model=model_id, max_tokens=max_tokens, system=SYSTEM_PROMPT, messages=messages)
    if tools:
        kw["tools"] = tools
    return anthropic_client.messages.create(**kw)

def call_gemini(model_id, messages):
    if not GEMINI_AVAILABLE or not gemini_client:
        raise Exception("Gemini not available")
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append(google_genai_types.Content(role=role, parts=[google_genai_types.Part(text=str(m["content"]))]))
    resp = gemini_client.models.generate_content(
        model=model_id, contents=contents,
        config=google_genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, max_output_tokens=8192),
    )
    output_tokens = 0
    try:
        if resp.usage_metadata:
            output_tokens = getattr(resp.usage_metadata, 'candidates_token_count', 0) or 0
    except Exception:
        pass
    return resp.text, output_tokens

def call_openai_compat(client, model_id, messages):
    if not client:
        raise Exception("AI provider not available")
    oai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        role = m["role"] if m["role"] in ("user", "assistant") else "user"
        content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
        oai_messages.append({"role": role, "content": content})
    resp = client.chat.completions.create(
        model=model_id,
        messages=oai_messages,
        max_completion_tokens=8192,
    )
    text = resp.choices[0].message.content or ""
    output_tokens = 0
    try:
        output_tokens = resp.usage.completion_tokens or 0
    except Exception:
        pass
    return text, output_tokens

SYSTEM_PROMPT = """You are Rux, a Roblox Studio and Luau expert AI assistant connected to a live Roblox Studio plugin via a tool bridge.

TOOLS:
- You have direct access to tools (read_script, write_script, list_scripts, get_script_tree, search_code, create_script, delete_script, snapshot_script, restore_script, diff_script, check_errors, get_instance_tree, get_properties, set_property, find_instance, get_selection, get_current_script, get_place_metadata, find_usages, get_output_log, get_error_log, create_checkpoint, list_checkpoints).
- CRITICAL: You may only call ONE tool per response. Never call multiple tools at once. Call one tool, wait for the result, then decide your next step.
- Always use list_scripts or get_script_tree first before trying to read a specific script by name.
- Prefer get_script_tree over get_instance_tree — get_instance_tree returns extremely large data. Use find_instance or get_script_tree instead.
- get_output_log and get_error_log may return empty results — tell the user to check the Studio Output window directly if needed.
- If a tool result is truncated, use more specific tools like read_script or find_instance.

CHECKPOINTS:
- A checkpoint is automatically created at the start of every agent task, capturing the state of scripts before they are modified.
- You may also call create_checkpoint(label, scripts) explicitly when you want to save the content of specific scripts at any point. Pass the script names and their current source code (which you have already read via read_script) in the scripts dict.
- Call list_checkpoints() to see what checkpoints are saved for the current session.
- Call restore_checkpoint(checkpoint_id) to retrieve saved script contents from a checkpoint; then use write_script for each script to apply the rollback.
- The user can also restore any checkpoint from the web UI to roll back changes.

RULES:
- Be precise, safe, and incremental.
- In agent mode, first produce a numbered plan before using tools.
- In chat mode, call tools directly — no plan needed.
- Prefer inspection before writing.
- Before editing any script, call read_script first so the original is captured by the checkpoint system.
- Keep changes minimal and explain what changed.
- If a tool returns an error, recover gracefully and try the next best step.
- Stop when the task is complete and give a concise summary.
- Never make up script contents or tool results — always use real tool output.
"""

TOOL_DEFINITIONS = [
    {"name": "read_script", "description": "Find a script by name and return its source code.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "write_script", "description": "Write code into an existing script by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "code": {"type": "string"}}, "required": ["name", "code"]}},
    {"name": "create_script", "description": "Create a new Script, LocalScript, or ModuleScript under a parent path.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "parent": {"type": "string"}}, "required": ["name", "type", "parent"]}},
    {"name": "delete_script", "description": "Delete a script by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "list_scripts", "description": "List all script names in the current place.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_script_tree", "description": "Get a JSON tree or list of scripts and their paths.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "check_errors", "description": "Attempt to detect syntax or script issues for a named script.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_output_log", "description": "Get recent output log lines available through the plugin.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_error_log", "description": "Get recent error log lines available through the plugin.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "search_code", "description": "Search all scripts for a query string.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find_usages", "description": "Search all scripts for usages of a variable or function name.", "input_schema": {"type": "object", "properties": {"variable_name": {"type": "string"}}, "required": ["variable_name"]}},
    {"name": "get_instance_tree", "description": "Get the Explorer instance tree. WARNING: Returns very large data. Prefer get_script_tree or find_instance.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_properties", "description": "Get properties for an instance path.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string"}}, "required": ["instance_path"]}},
    {"name": "set_property", "description": "Set a property on an instance path.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string"}, "property": {"type": "string"}, "value": {}}, "required": ["instance_path", "property", "value"]}},
    {"name": "find_instance", "description": "Find an instance anywhere in the game by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_selection", "description": "Return the current Explorer selection.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_current_script", "description": "Return the currently selected or active script name and source if available.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_place_metadata", "description": "Return game name, place id, and version.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "snapshot_script", "description": "Save a snapshot of a script before modification.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "diff_script", "description": "Show differences against the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "restore_script", "description": "Restore a script to the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "create_checkpoint", "description": "Save a named checkpoint of one or more scripts so the user can roll back later. Call this before or after making edits. Pass the script names and their current source code.", "input_schema": {"type": "object", "properties": {"label": {"type": "string", "description": "A short label, e.g. 'Before fixing PlayerController'"}, "scripts": {"type": "object", "description": "Dict mapping script names to their source code", "additionalProperties": {"type": "string"}}}, "required": ["label", "scripts"]}},
    {"name": "list_checkpoints", "description": "List all checkpoints saved for the current session. Returns id, label, timestamp, and number of scripts in each checkpoint.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "restore_checkpoint", "description": "Retrieve the saved script contents from a checkpoint by ID so you can restore them. After getting the result, call write_script for each script to apply the rollback.", "input_schema": {"type": "object", "properties": {"checkpoint_id": {"type": "string", "description": "The checkpoint ID (from list_checkpoints)"}}, "required": ["checkpoint_id"]}},
]

SERVER_SIDE_TOOLS = {"create_checkpoint", "list_checkpoints", "restore_checkpoint"}

# ═══ CHECKPOINT HELPERS ═══

def _extract_script_source(result_data):
    if isinstance(result_data, dict):
        for key in ("source", "code", "content", "script", "text"):
            val = result_data.get(key)
            if val and isinstance(val, str):
                return val
    return None


def _resolve_server_tool(session, tc):
    name = tc.get("name")
    args = tc.get("arguments", {})
    user_id = session.get("user_id")
    if name == "create_checkpoint":
        label = args.get("label", "Checkpoint")
        scripts = args.get("scripts", {})
        ckpt_id = "ckpt-" + str(uuid.uuid4())
        existing_scripts = {}
        if user_id:
            auto_id = session.get("current_checkpoint_id")
            if auto_id:
                existing = store.get_checkpoint(user_id, auto_id)
                if existing:
                    existing_scripts = existing.get("scripts", {})
        merged = dict(existing_scripts)
        merged.update(scripts)
        ckpt_data = {
            "id": ckpt_id, "label": label,
            "created_at": int(time.time() * 1000),
            "scripts": merged,
            "session_id": session.get("_session_id"),
        }
        if user_id:
            store.save_checkpoint(user_id, ckpt_id, ckpt_data)
        return {"ok": True, "checkpoint_id": ckpt_id, "label": label, "scripts_saved": len(merged)}
    elif name == "list_checkpoints":
        if not user_id:
            return {"checkpoints": []}
        ckpts = store.get_checkpoints(user_id)
        sid = session.get("_session_id")
        result_list = sorted(
            [
                {"id": c["id"], "label": c["label"], "created_at": c["created_at"], "scripts_count": len(c.get("scripts", {}))}
                for c in ckpts.values()
                if not sid or c.get("session_id") == sid
            ],
            key=lambda x: x["created_at"], reverse=True,
        )
        return {"checkpoints": result_list[:20]}
    elif name == "restore_checkpoint":
        checkpoint_id = args.get("checkpoint_id", "")
        if not user_id or not checkpoint_id:
            return {"error": "Missing checkpoint_id or not authenticated"}
        ckpt = store.get_checkpoint(user_id, checkpoint_id)
        if not ckpt:
            return {"error": f"Checkpoint '{checkpoint_id}' not found"}
        scripts = ckpt.get("scripts", {})
        if not scripts:
            return {"error": "Checkpoint has no saved scripts to restore"}
        return {
            "ok": True,
            "checkpoint_id": checkpoint_id,
            "label": ckpt["label"],
            "scripts": scripts,
            "message": f"Checkpoint has {len(scripts)} script(s). Use write_script for each script name in 'scripts' to restore them.",
        }
    return {"ok": True}


def _continue_agent_with_result(session, tc, result_data):
    user_id = session.get("user_id")
    mk = session.get("model_key", DEFAULT_MODEL)
    mi = resolve_model(mk)
    cpt = mi["credit_per_token"]
    if mi["provider"] != "anthropic" or not anthropic_client:
        session["status"] = "error"
        session["pending_tool_call"] = None
        return None
    prior = session.get("agent_messages", [])
    tool_result_str = json.dumps(result_data, ensure_ascii=False)
    tr = {"type": "tool_result", "tool_use_id": tc["id"], "content": tool_result_str}
    cont = prior + [{"role": "user", "content": [tr]}]
    try:
        r = call_anthropic(mi["id"], cont, tools=TOOL_DEFINITIONS)
        first_tc, _, ft = extract_tool_info(r.content)
        if first_tc:
            assistant_content = build_assistant_content(r.content, first_tc["id"])
        else:
            assistant_content = content_blocks_to_dicts(r.content)
        session["agent_messages"] = cont + [{"role": "assistant", "content": assistant_content}]
        session["step_count"] = session.get("step_count", 0) + 1
        output_tokens = r.usage.output_tokens
        cost = round(output_tokens * cpt, 6)
        if user_id:
            store.deduct_credits(user_id, cost)
        session["accumulated_cost"] = session.get("accumulated_cost", 0) + cost
        if ft:
            session["accumulated_reply"] = session.get("accumulated_reply", "") + ft
        if first_tc:
            session["pending_tool_call"] = first_tc
            session["status"] = "running"
            return first_tc
        else:
            session["pending_tool_call"] = None
            session["status"] = "done"
            session["latest_reply"] = session.get("accumulated_reply", "") or ft
            return None
    except Exception as e:
        print(f"[Rux] _continue_agent_with_result error: {e}")
        session["status"] = "error"
        session["pending_tool_call"] = None
        return None


# ═══ ADMIN ROUTES ═══

@app.route("/admin/api/users", methods=["GET"])
@require_admin
def admin_get_users():
    users = store.get_all_users_with_credits()
    return jsonify(users)

@app.route("/admin/api/credits", methods=["POST"])
@require_admin
def admin_set_credits():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    balance = data.get("balance")
    max_credit = data.get("max_credit")
    if not user_id or balance is None or max_credit is None:
        return jsonify({"error": "Missing fields"}), 400
    store.set_user_credits(user_id, balance, max_credit)
    return jsonify({"ok": True})

@app.route("/admin/api/block", methods=["POST"])
@require_admin
def admin_block_user():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    if user_id == OWNER_ID:
        return jsonify({"error": "Cannot block owner"}), 403
    if store.is_admin(user_id) and request.user.get("id") != OWNER_ID:
        return jsonify({"error": "Only owner can block admins"}), 403
    store.block_user(user_id)
    return jsonify({"ok": True})

@app.route("/admin/api/unblock", methods=["POST"])
@require_admin
def admin_unblock_user():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    store.unblock_user(user_id)
    return jsonify({"ok": True})

@app.route("/admin/api/promote", methods=["POST"])
@require_owner
def admin_promote_user():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    store.add_admin(user_id)
    return jsonify({"ok": True})

@app.route("/admin/api/demote", methods=["POST"])
@require_owner
def admin_demote_user():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    if user_id == OWNER_ID:
        return jsonify({"error": "Cannot demote owner"}), 403
    store.remove_admin(user_id)
    return jsonify({"ok": True})

@app.route("/admin/api/user/<user_id>", methods=["DELETE"])
@require_owner
def admin_delete_user(user_id):
    if user_id == OWNER_ID:
        return jsonify({"error": "Cannot delete owner"}), 403
    if not store.delete_user(user_id):
        return jsonify({"error": "Failed to delete"}), 400
    return jsonify({"ok": True})

@app.route("/admin/api/maintenance", methods=["GET"])
@require_admin
def admin_get_maintenance():
    return jsonify({"enabled": store.is_maintenance()})

@app.route("/admin/api/maintenance", methods=["POST"])
@require_admin
def admin_set_maintenance():
    data = request.get_json(force=True)
    enabled = data.get("enabled", False)
    store.set_maintenance(enabled)
    return jsonify({"ok": True, "enabled": enabled})

@app.route("/admin/api/export", methods=["GET"])
@require_admin
def admin_export():
    data = store.export_all()
    return jsonify(data)

@app.route("/admin/api/import", methods=["POST"])
@require_owner
def admin_import():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400
    store.import_all(data)
    return jsonify({"ok": True})

@app.route("/admin")
def admin_page():
    return render_template("admin.html")


# ═══ AUTH ROUTES ═══
@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    roblox_id = data.get("roblox_id") or ""

    if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Invalid email"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    existing = store.get_user_by_email(email)
    if existing:
        return jsonify({"error": "Email already registered"}), 400

    user_id = str(uuid.uuid4())
    store.save_user(user_id, email, hash_password(password), roblox_id)
    store.init_credits(user_id)

    token = str(uuid.uuid4())
    store.save_session(token, user_id)

    return jsonify({"token": token, "user": {"id": user_id, "email": email}})

@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = store.get_user_by_email(email)
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401

    if not verify_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401

    token = str(uuid.uuid4())
    store.save_session(token, user["id"])

    return jsonify({"token": token, "user": {"id": user["id"], "email": user["email"]}})

@app.route("/auth/me", methods=["GET"])
@require_auth
def auth_me():
    user = request.user
    balance, last_updated = store.get_credits(user["id"])
    next_at = last_updated + store.CREDIT_INTERVAL_MS if last_updated > 0 else 0

    active_session = store.get_user_plugin(user["id"])
    session_id = None
    now = time.time()
    if active_session:
        pid = active_session.get("plugin_id")
        with plugin_registry_lock:
            if pid and pid in plugin_registry and now - plugin_registry[pid]["last_seen"] < 15:
                session_id = active_session.get("session_id")

    update_web_heartbeat(user["id"])

    return jsonify({
        "user": {"id": user["id"], "email": user["email"]},
        "credits": {"balance": round(balance, 2), "max": store.MAX_CREDITS, "next_credit_at": next_at},
        "session_id": session_id,
    })

@app.route("/auth/logout", methods=["POST"])
@require_auth
def auth_logout():
    user = request.user
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    store.delete_session(token)
    clear_web_heartbeat(user["id"])
    return jsonify({"ok": True})

# ═══ WEB HEARTBEAT ROUTE ═══
@app.route("/web/heartbeat", methods=["POST"])
@require_auth
def web_heartbeat():
    user = request.user
    update_web_heartbeat(user["id"])

    active_session = store.get_user_plugin(user["id"])
    plugin_connected = False
    session_id = None
    if active_session:
        pid = active_session.get("plugin_id")
        with plugin_registry_lock:
            if pid and pid in plugin_registry and time.time() - plugin_registry[pid]["last_seen"] < 15:
                plugin_connected = True
                session_id = active_session.get("session_id")

    balance, _ = store.get_credits(user["id"])
    return jsonify({
        "ok": True,
        "plugin_connected": plugin_connected,
        "session_id": session_id,
        "credits": round(balance, 2),
    })

# ═══ WEB DISCONNECT ROUTE ═══
@app.route("/web/disconnect", methods=["POST"])
@require_auth
def web_disconnect():
    user = request.user
    active_session = store.get_user_plugin(user["id"])
    if active_session:
        pid = active_session.get("plugin_id")
        with plugin_registry_lock:
            if pid and pid in plugin_registry:
                plugin_registry[pid]["status"] = "disconnected_by_web"
        store.delete_user_plugin(user["id"])
    clear_web_heartbeat(user["id"])
    return jsonify({"ok": True})

# ═══ CONNECTION ROUTES ═══
@app.route("/connect/code", methods=["GET"])
@require_auth
def connect_code():
    clean_expired_codes()
    code = generate_code()
    session_id = str(uuid.uuid4())
    with pending_connections_lock:
        pending_connections[code] = {
            "user_id": request.user["id"],
            "session_id": session_id,
            "created_at": int(time.time() * 1000),
        }
    return jsonify({"code": code, "session_id": session_id})

@app.route("/plugin/connect", methods=["POST"])
def plugin_connect():
    data = request.get_json(force=True)
    plugin_id = data.get("plugin_id")
    creator_id = data.get("creator_id")
    code = (data.get("code") or "").strip().upper()
    if not plugin_id:
        return jsonify({"ok": False, "error": "Missing plugin_id"}), 400

    session_id = None
    method = None
    user_id = None

    if code:
        clean_expired_codes()
        with pending_connections_lock:
            pending = pending_connections.get(code)
            if pending:
                now = int(time.time() * 1000)
                if now - pending["created_at"] <= CODE_EXPIRY_MS:
                    session_id = pending["session_id"]
                    method = "code"
                    user_id = pending["user_id"]
                    store.save_user_plugin(user_id, plugin_id, session_id)
                    del pending_connections[code]

    if not session_id and creator_id:
        u = store.get_user_by_roblox_id(creator_id)
        if u:
            user_id = u["id"]
            existing = store.get_user_plugin(user_id)
            if existing and existing.get("session_id"):
                session_id = existing["session_id"]
                method = "auto_reuse"
                store.save_user_plugin(user_id, plugin_id, session_id)
            else:
                session_id = str(uuid.uuid4())
                method = "auto"
                store.save_user_plugin(user_id, plugin_id, session_id)

    if not session_id:
        return jsonify({"ok": False, "error": "Invalid or expired code"}), 400

    with plugin_registry_lock:
        plugin_registry[plugin_id] = {
            "session_id": session_id, "plugin_id": plugin_id,
            "last_seen": time.time(), "status": "connected",
            "user_id": user_id,
        }

    return jsonify({"ok": True, "session_id": session_id, "method": method})

@app.route("/plugin/disconnect", methods=["POST"])
def plugin_disconnect():
    data = request.get_json(force=True)
    pid = data.get("plugin_id")
    if pid:
        with plugin_registry_lock:
            info = plugin_registry.get(pid)
            uid = info.get("user_id") if info else None
            if pid in plugin_registry:
                del plugin_registry[pid]
        if uid:
            store.delete_user_plugin(uid)
    return jsonify({"ok": True})

# ═══ AI ROUTES ═══
@app.route("/ai", methods=["POST"])
@require_auth
def ai():
    user = request.user
    balance, _ = store.get_credits(user["id"])
    if balance <= 0:
        balance2, last_upd = store.get_credits(user["id"])
        next_at = last_upd + store.CREDIT_INTERVAL_MS if last_upd > 0 else 0
        return jsonify({
            "error": "no_credits", "balance": round(balance2, 2),
            "next_credit_at": next_at,
        }), 403

    data = request.get_json(force=True)
    session_id = data.get("session_id") or str(uuid.uuid4())
    mode = data.get("mode", "chat")
    user_message = data.get("message", "")
    model_key = data.get("model", DEFAULT_MODEL)
    conversation_history = data.get("conversation_history", [])
    context = build_context(data)

    session = get_session(session_id)
    session["conversation"] = conversation_history
    session["latest_context"] = context
    session["model_key"] = model_key
    session["user_id"] = user["id"]
    session["accumulated_reply"] = ""
    session["accumulated_cost"] = 0.0

    mi = resolve_model(model_key)
    mid, prov, cpt = mi["id"], mi["provider"], mi["credit_per_token"]

    try:
        if mode == "chat":
            msgs = build_chat_messages(session, user_message, context)
            if prov == "anthropic":
                r = call_anthropic(mid, msgs, tools=TOOL_DEFINITIONS)
                output_tokens = r.usage.output_tokens
                cost = round(output_tokens * cpt, 6)
                balance, _ = store.deduct_credits(user["id"], cost)

                first_tc, all_tcs, reply_text = extract_tool_info(r.content)

                if first_tc:
                    assistant_content = build_assistant_content(r.content, first_tc["id"])
                    if len(all_tcs) > 1:
                        print(f"[Rux] WARNING: {len(all_tcs)} tool_use blocks, executing first: {first_tc['name']}")
                        reply_text += f"\n\n[Calling {len(all_tcs)} tools one at a time. Starting with {first_tc['name']}.]"

                    session["pending_tool_call"] = first_tc
                    session["agent_messages"] = msgs + [{"role": "assistant", "content": assistant_content}]
                    session["status"] = "running"
                    session["latest_reply"] = ""
                    session["accumulated_cost"] = cost
                    return jsonify({
                        "session_id": session_id, "reply": reply_text or "",
                        "tool_calls": [first_tc], "plan": None,
                        "status": "tool_requested", "model": mi["label"],
                        "credits": round(balance, 2), "tokens_used": output_tokens,
                    })

                session["latest_reply"] = reply_text
                session["status"] = "done"
                return jsonify({
                    "session_id": session_id, "reply": reply_text,
                    "tool_calls": [], "plan": None, "status": "done",
                    "model": mi["label"], "credits": round(balance, 2),
                    "tokens_used": output_tokens,
                })
            elif prov in ("openai", "openrouter"):
                client = openai_client if prov == "openai" else openrouter_client
                reply, output_tokens = call_openai_compat(client, mid, msgs)
                cost = round(output_tokens * cpt, 6)
                balance, _ = store.deduct_credits(user["id"], cost)
                session["latest_reply"] = reply
                session["status"] = "done"
                return jsonify({
                    "session_id": session_id, "reply": reply,
                    "tool_calls": [], "plan": None, "status": "done",
                    "model": mi["label"], "credits": round(balance, 2),
                    "tokens_used": output_tokens,
                })
            else:
                reply, output_tokens = call_gemini(mid, msgs)
                cost = round(output_tokens * cpt, 6)
                balance, _ = store.deduct_credits(user["id"], cost)
                session["latest_reply"] = reply
                session["status"] = "done"
                return jsonify({
                    "session_id": session_id, "reply": reply,
                    "tool_calls": [], "plan": None, "status": "done",
                    "model": mi["label"], "credits": round(balance, 2),
                    "tokens_used": output_tokens,
                })

        elif mode == "agent":
            if prov != "anthropic":
                return jsonify({
                    "session_id": session_id,
                    "reply": "Agent mode requires a Claude model. Please switch to Claude Sonnet or Claude Opus.",
                    "tool_calls": [], "plan": None, "status": "done",
                    "model": mi["label"], "credits": round(balance, 2), "tokens_used": 0,
                })
            session["approved"] = False
            session["step_count"] = 0
            session["pending_tool_call"] = None
            session["status"] = "planning"
            pm = build_chat_messages(session, user_message, context)
            pm.append({"role": "user", "content": "Produce a numbered execution plan only. Do not call any tools yet."})
            r = call_anthropic(mid, pm, max_tokens=2000)
            output_tokens = r.usage.output_tokens
            cost = round(output_tokens * cpt, 6)
            balance, _ = store.deduct_credits(user["id"], cost)
            plan = "".join(b.text for b in r.content if hasattr(b, 'type') and b.type == "text")
            session["plan"] = plan
            session["agent_messages"] = pm + [{"role": "assistant", "content": content_blocks_to_dicts(r.content)}]
            session["accumulated_cost"] = cost
            return jsonify({
                "session_id": session_id, "reply": "Plan generated.",
                "tool_calls": [], "plan": plan,
                "status": "awaiting_approval", "model": mi["label"],
                "credits": round(balance, 2), "tokens_used": output_tokens,
            })
    except Exception as e:
        print(f"[Rux] /ai error: {traceback.format_exc()}")
        return jsonify({
            "session_id": session_id, "reply": f"Failed: {e}",
            "tool_calls": [], "plan": None, "status": "error",
            "credits": round(balance, 2), "tokens_used": 0,
        }), 500

@app.route("/ai/result/<session_id>", methods=["GET"])
@require_auth
def ai_result(session_id):
    session = get_session(session_id)
    tc = session.get("pending_tool_call")
    balance, _ = store.get_credits(request.user["id"])
    return jsonify({
        "session_id": session_id,
        "status": session.get("status", "idle"),
        "reply": session.get("latest_reply", ""),
        "pending_tool_call": tc,
        "tool_calls": [tc] if tc else [],
        "credits": round(balance, 2),
    })

@app.route("/ai/approve", methods=["POST"])
@require_auth
def approve_agent():
    user = request.user
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    session = get_session(session_id)
    am = data.get("model")
    if am:
        session["model_key"] = am
    session["approved"] = True
    session["status"] = "running"
    session["accumulated_reply"] = ""
    mi = resolve_model(session.get("model_key", DEFAULT_MODEL))
    cpt = mi["credit_per_token"]
    if mi["provider"] != "anthropic":
        session["status"] = "error"
        return jsonify({
            "session_id": session_id, "reply": "Agent mode requires a Claude model.",
            "tool_calls": [], "plan": session.get("plan"),
            "status": "error", "credits": 0, "tokens_used": 0,
        }), 400

    balance, _ = store.get_credits(user["id"])
    if balance <= 0:
        return jsonify({
            "session_id": session_id, "reply": "No credits remaining.",
            "tool_calls": [], "plan": session.get("plan"),
            "status": "error", "credits": round(balance, 2), "tokens_used": 0,
        }), 403

    try:
        # Auto-create a checkpoint before execution starts
        agent_msgs = session.get("agent_messages", [])
        user_msg_label = "Agent run"
        for m in agent_msgs:
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    if "User message:" in content:
                        user_msg_label = content.split("User message:")[1].split("\n")[0].strip()[:60]
                    else:
                        user_msg_label = content[:60]
                    break
        auto_ckpt_id = "ckpt-" + str(uuid.uuid4())
        store.save_checkpoint(user["id"], auto_ckpt_id, {
            "id": auto_ckpt_id,
            "label": f"Before: {user_msg_label}",
            "created_at": int(time.time() * 1000),
            "scripts": {},
            "auto": True,
            "session_id": session_id,
        })
        session["current_checkpoint_id"] = auto_ckpt_id
        session["script_cache"] = {}
        session["restore_queue"] = []

        prior = session.get("agent_messages", [])
        prior.append({"role": "user", "content": "The plan is approved. Start executing now. Use one tool at a time."})
        r = call_anthropic(mi["id"], prior, tools=TOOL_DEFINITIONS)
        session["agent_messages"] = prior + [{"role": "assistant", "content": content_blocks_to_dicts(r.content)}]

        output_tokens = r.usage.output_tokens
        cost = round(output_tokens * cpt, 6)
        balance, _ = store.deduct_credits(user["id"], cost)
        session["accumulated_cost"] = session.get("accumulated_cost", 0) + cost

        first_tc, all_tcs, ft = extract_tool_info(r.content)

        if first_tc:
            assistant_content = build_assistant_content(r.content, first_tc["id"])
            session["agent_messages"][-1] = {"role": "assistant", "content": assistant_content}
            if len(all_tcs) > 1:
                print(f"[Rux] WARNING: Agent returned {len(all_tcs)} tool_use blocks, executing first: {first_tc['name']}")
            session["pending_tool_call"] = first_tc
            return jsonify({
                "session_id": session_id, "reply": ft or "Executing.",
                "tool_calls": [first_tc], "plan": session["plan"],
                "status": "tool_requested", "credits": round(balance, 2),
                "tokens_used": output_tokens,
            })
        session["latest_reply"] = ft
        session["status"] = "done"
        return jsonify({
            "session_id": session_id, "reply": ft,
            "tool_calls": [], "plan": session["plan"],
            "status": "done", "credits": round(balance, 2),
            "tokens_used": output_tokens,
        })
    except Exception as e:
        print(f"[Rux] /ai/approve error: {traceback.format_exc()}")
        return jsonify({
            "session_id": session_id, "reply": f"Failed: {e}",
            "tool_calls": [], "plan": session.get("plan"),
            "status": "error", "credits": round(balance, 2), "tokens_used": 0,
        }), 500

# ═══ PLUGIN ROUTES ═══
@app.route("/plugin/heartbeat", methods=["POST"])
def plugin_heartbeat():
    data = request.get_json(force=True)
    pid = data.get("plugin_id")
    sid = data.get("session_id")
    if pid:
        with plugin_registry_lock:
            if pid not in plugin_registry:
                plugin_registry[pid] = {"session_id": sid, "plugin_id": pid, "last_seen": 0, "status": "connected", "user_id": None}
            plugin_registry[pid]["last_seen"] = time.time()
            plugin_registry[pid]["status"] = "connected"
            plugin_registry[pid]["selected_instance"] = data.get("selected_instance")
    return jsonify({"ok": True})

@app.route("/plugin/poll", methods=["POST"])
def plugin_poll():
    data = request.get_json(force=True)
    sid = data.get("session_id")
    pid = data.get("plugin_id")
    session = get_session(sid)

    web_connected = False
    plugin_disconnected = False
    with plugin_registry_lock:
        info = plugin_registry.get(pid)
        if info:
            if info.get("user_id"):
                web_connected = is_web_active(info["user_id"])
            if info.get("status") == "disconnected_by_web":
                plugin_disconnected = True
                info["status"] = "connected"

    # Resolve server-side tools without plugin involvement
    pending_tc = session.get("pending_tool_call")
    resolved = 0
    while pending_tc and pending_tc.get("name") in SERVER_SIDE_TOOLS and resolved < 5:
        synthetic = _resolve_server_tool(session, pending_tc)
        pending_tc = _continue_agent_with_result(session, pending_tc, synthetic)
        resolved += 1

    # If no pending tool call but restore queue has items, pop next
    if not session.get("pending_tool_call") and session.get("restore_queue") and session.get("status") == "restoring":
        next_write = session["restore_queue"].pop(0)
        session["pending_tool_call"] = next_write

    return jsonify({
        "status_message": session["status"],
        "tool_call": session.get("pending_tool_call"),
        "web_connected": web_connected,
        "disconnected": plugin_disconnected,
    })

@app.route("/plugin/tool_result", methods=["POST"])
def plugin_tool_result():
    data = request.get_json(force=True)
    sid = data.get("session_id")
    session = get_session(sid)
    user_id = session.get("user_id")
    mk = session.get("model_key", DEFAULT_MODEL)
    mi = resolve_model(mk)
    cpt = mi["credit_per_token"]

    # Restore mode: handle queue advance before any provider/step checks
    if session.get("status") == "restoring":
        balance = 0
        if user_id:
            balance, _ = store.get_credits(user_id)
        restore_queue = session.get("restore_queue", [])
        pc_restore = session.get("pending_tool_call")
        session["pending_tool_call"] = None
        if restore_queue:
            next_write = restore_queue.pop(0)
            session["pending_tool_call"] = next_write
            return jsonify({"reply": "Restoring...", "status": "tool_requested", "tool_call": next_write, "credits": round(balance, 2), "tokens_used": 0})
        else:
            session["status"] = "done"
            n = session.get("restore_scripts_count", 0)
            session["latest_reply"] = f"Checkpoint restored: {n} script{'s' if n != 1 else ''} rolled back."
            return jsonify({"reply": session["latest_reply"], "status": "done", "credits": round(balance, 2), "tokens_used": 0})

    if mi["provider"] != "anthropic":
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({"reply": "Tool use only supported with Claude models.", "status": "error"}), 400

    if session["step_count"] >= MAX_AGENT_STEPS:
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({"reply": "Max steps reached.", "status": "error"}), 400

    pc = session.get("pending_tool_call")
    if not pc:
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({"reply": "No pending tool call. Session expired — start a new conversation.", "status": "error"}), 200

    session["step_count"] += 1
    session["pending_tool_call"] = None
    try:
        prior = session.get("agent_messages", [])
        if len(prior) == 0:
            session["status"] = "error"
            return jsonify({"reply": "Session expired. Start a new conversation.", "status": "error"}), 200

        tool_result_data = data.get("tool_result", {})

        # ── Checkpoint: intercept read_script results ──
        tool_name = pc.get("name", "")
        tool_args = pc.get("arguments", {})
        ckpt_id = session.get("current_checkpoint_id")
        if tool_name == "read_script" and ckpt_id and user_id:
            script_name = tool_args.get("name", "")
            source = _extract_script_source(tool_result_data)
            if script_name and source:
                session.setdefault("script_cache", {})[script_name] = source
                ckpt = store.get_checkpoint(user_id, ckpt_id)
                if ckpt and script_name not in ckpt.get("scripts", {}):
                    ckpt.setdefault("scripts", {})[script_name] = source
                    store.save_checkpoint(user_id, ckpt_id, ckpt)
        elif tool_name == "write_script" and ckpt_id and user_id:
            script_name = tool_args.get("name", "")
            new_code = tool_args.get("code", "")
            before_code = session.get("script_cache", {}).get(script_name)
            if script_name and before_code:
                ckpt = store.get_checkpoint(user_id, ckpt_id)
                if ckpt and script_name not in ckpt.get("scripts", {}):
                    ckpt.setdefault("scripts", {})[script_name] = before_code
                    store.save_checkpoint(user_id, ckpt_id, ckpt)
            if script_name and new_code:
                session.setdefault("script_cache", {})[script_name] = new_code

        tool_result_data = truncate_tool_result(tool_result_data)
        tool_result_str = json.dumps(tool_result_data, ensure_ascii=False)

        tr = {"type": "tool_result", "tool_use_id": pc["id"], "content": tool_result_str}
        cont = prior + [{"role": "user", "content": [tr]}]
        r = call_anthropic(mi["id"], cont, tools=TOOL_DEFINITIONS)

        first_tc, all_tcs, ft = extract_tool_info(r.content)

        if first_tc:
            assistant_content = build_assistant_content(r.content, first_tc["id"])
        else:
            assistant_content = content_blocks_to_dicts(r.content)

        session["agent_messages"] = cont + [{"role": "assistant", "content": assistant_content}]

        output_tokens = r.usage.output_tokens
        cost = round(output_tokens * cpt, 6)
        if user_id:
            balance, _ = store.deduct_credits(user_id, cost)
        else:
            balance = 0
        session["accumulated_cost"] = session.get("accumulated_cost", 0) + cost

        if ft:
            session["accumulated_reply"] = session.get("accumulated_reply", "") + ft

        if first_tc:
            if len(all_tcs) > 1:
                print(f"[Rux] WARNING: tool_result continuation returned {len(all_tcs)} tool_use blocks, executing first: {first_tc['name']}")
            session["pending_tool_call"] = first_tc
            session["status"] = "running"
            return jsonify({
                "reply": ft or "Tool processed.",
                "status": "tool_requested",
                "tool_call": first_tc,
                "credits": round(balance, 2),
                "tokens_used": output_tokens,
            })

        session["status"] = "done"
        final_reply = session.get("accumulated_reply", "") or ft
        session["latest_reply"] = final_reply
        session["accumulated_reply"] = ""
        return jsonify({
            "reply": final_reply, "status": "done",
            "credits": round(balance, 2), "tokens_used": output_tokens,
        })
    except Exception as e:
        error_str = str(e)
        print(f"[Rux] /plugin/tool_result error: {traceback.format_exc()}")
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({
            "reply": f"Error processing tool result: {error_str[:300]}",
            "status": "error",
        }), 200

# ═══ CONVERSATION ROUTES ═══
@app.route("/api/conversations", methods=["GET"])
@require_auth
def get_conversations():
    user = request.user
    data = store.get_conv_list(user["id"])
    return jsonify(data)

@app.route("/api/conversations/<conv_id>", methods=["GET"])
@require_auth
def get_conversation(conv_id):
    user = request.user
    conv_list = store.get_conv_list(user["id"])
    if not conv_list or conv_id not in conv_list:
        return jsonify({"error": "Not found"}), 404
    data = store.get_conv(conv_id)
    if not data:
        return jsonify({"error": "Not found"}), 404
    return jsonify(data)

@app.route("/api/conversations", methods=["POST"])
@require_auth
def save_conversation():
    user = request.user
    data = request.get_json(force=True)
    conv_id = data.get("id")
    if not conv_id:
        return jsonify({"error": "Missing id"}), 400

    conv_data = {
        "id": conv_id,
        "title": data.get("title", "Conversation"),
        "messages": data.get("messages", []),
        "history": data.get("history", []),
        "mode": data.get("mode", "chat"),
        "model": data.get("model", "sonnet"),
        "sessionId": data.get("sessionId"),
        "updatedAt": data.get("updatedAt", int(time.time() * 1000)),
    }
    store.save_conv(user["id"], conv_id, conv_data)
    return jsonify({"ok": True})

@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
@require_auth
def delete_conversation(conv_id):
    user = request.user
    store.delete_conv(user["id"], conv_id)
    return jsonify({"ok": True})

# ═══ CHECKPOINT ROUTES ═══
@app.route("/api/checkpoints", methods=["GET"])
@require_auth
def get_checkpoints_route():
    user = request.user
    filter_session_id = request.args.get("session_id")
    ckpts = store.get_checkpoints(user["id"])
    result = []
    for c in ckpts.values():
        if filter_session_id and c.get("session_id") != filter_session_id:
            continue
        script_names = list(c.get("scripts", {}).keys())
        result.append({
            "id": c["id"], "label": c["label"], "created_at": c["created_at"],
            "scripts_count": len(script_names), "auto": c.get("auto", False),
            "session_id": c.get("session_id"), "script_names": script_names,
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify(result)


@app.route("/api/checkpoints", methods=["POST"])
@require_auth
def create_checkpoint_route():
    user = request.user
    data = request.get_json(force=True)
    label = data.get("label", "Manual checkpoint")
    scripts = data.get("scripts", {})
    ckpt_id = "ckpt-" + str(uuid.uuid4())
    ckpt_data = {
        "id": ckpt_id,
        "label": label,
        "created_at": int(time.time() * 1000),
        "scripts": scripts,
        "auto": False,
    }
    store.save_checkpoint(user["id"], ckpt_id, ckpt_data)
    return jsonify({"ok": True, "id": ckpt_id})


@app.route("/api/checkpoints/<ckpt_id>", methods=["DELETE"])
@require_auth
def delete_checkpoint_route(ckpt_id):
    user = request.user
    ok = store.delete_checkpoint(user["id"], ckpt_id)
    return jsonify({"ok": ok})


@app.route("/api/checkpoints/<ckpt_id>/restore", methods=["POST"])
@require_auth
def restore_checkpoint_route(ckpt_id):
    user = request.user
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400
    ckpt = store.get_checkpoint(user["id"], ckpt_id)
    if not ckpt:
        return jsonify({"error": "Checkpoint not found"}), 404
    scripts = ckpt.get("scripts", {})
    if not scripts:
        return jsonify({"error": "Checkpoint has no saved scripts"}), 400
    session = get_session(session_id)
    # Strict: session must be owned by this user (reject if unset or mismatched)
    if session.get("user_id") != user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    restore_calls = [
        {"id": "restore-" + str(uuid.uuid4()), "name": "write_script", "arguments": {"name": sname, "code": code}}
        for sname, code in scripts.items()
    ]
    first = restore_calls.pop(0) if restore_calls else None
    if not first:
        return jsonify({"error": "No scripts to restore"}), 400
    session["restore_queue"] = restore_calls
    session["restore_scripts_count"] = len(scripts)
    session["pending_tool_call"] = first
    session["status"] = "restoring"
    session["latest_reply"] = ""
    session["accumulated_reply"] = ""
    return jsonify({"ok": True, "scripts_count": len(scripts), "label": ckpt["label"]})


# ═══ STATUS / MODELS / PAGES ═══
@app.route("/status", methods=["GET"])
def get_status():
    now = time.time()
    with plugin_registry_lock:
        active = [p for p in plugin_registry.values() if now - p["last_seen"] < 15]
    latest = active[0] if active else None
    return jsonify({
        "plugin_connected": bool(active),
        "plugin_count": len(active),
        "selected_instance": latest["selected_instance"] if latest else None,
    })

@app.route("/models", methods=["GET"])
def get_models():
    return jsonify(MODELS)

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/app")
def chat_app():
    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)