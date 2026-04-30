import os
import json
import uuid
import time
import re
import secrets
import logging
import traceback

logging.basicConfig(level=logging.INFO)
from urllib.request import urlopen
from flask import Flask, request, jsonify, render_template
from threading import Lock
from functools import wraps

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
WEB_HEARTBEAT_TIMEOUT = 180  # seconds — web tab considered offline after this
PLUGIN_OFFLINE_AFTER = 45    # seconds — Studio plugin considered offline after this

MODELS = {
    "gemini-flash": {"id": "gemini-2.5-flash", "provider": "google", "label": "Gemini Flash", "badge": "Fast", "credit_per_token": 0.0001},
    "gemini-pro": {"id": "gemini-2.5-pro", "provider": "google", "label": "Gemini Pro", "badge": "Smart", "credit_per_token": 0.0003},
    "sonnet": {"id": "claude-sonnet-4-6", "provider": "anthropic", "label": "Claude Sonnet", "badge": "Balanced", "credit_per_token": 0.0005},
    "opus": {"id": "claude-opus-4-6", "provider": "anthropic", "label": "Claude Opus", "badge": "Powerful", "credit_per_token": 0.001},
    "haiku": {"id": "claude-haiku-4-5", "provider": "anthropic", "label": "Claude Haiku", "badge": "Fast", "credit_per_token": 0.0002},
    "gpt-5": {"id": "gpt-5", "provider": "openai", "label": "GPT-5", "badge": "Smart", "credit_per_token": 0.0005},
    "gpt-5-mini": {"id": "gpt-5-mini", "provider": "openai", "label": "GPT-5 Mini", "badge": "Fast", "credit_per_token": 0.0002},
    "gpt-4-1": {"id": "gpt-4.1", "provider": "openai", "label": "GPT-4.1", "badge": "Strong", "credit_per_token": 0.0004},
    "qwen-coder": {"id": "qwen/qwen3-coder", "provider": "openrouter", "label": "Qwen3 Coder", "badge": "Smart", "credit_per_token": 0.0003},
    "glm-5": {"id": "z-ai/glm-5.1", "provider": "openrouter", "label": "GLM-5.1", "badge": "Powerful", "credit_per_token": 0.0007},
    "grok-4": {"id": "x-ai/grok-4", "provider": "openrouter", "label": "Grok 4", "badge": "Smart", "credit_per_token": 0.0004},
    "deepseek": {"id": "deepseek/deepseek-chat", "provider": "openrouter", "label": "DeepSeek", "badge": "Free", "credit_per_token": 0},
    "llama-70b": {"id": "meta-llama/llama-3.3-70b-instruct", "provider": "openrouter", "label": "Llama 3.3 70B", "badge": "Free", "credit_per_token": 0},
    "max": {"id": "claude-sonnet-4-6", "provider": "anthropic", "label": "Max (multi-model)", "badge": "Max", "credit_per_token": 0.0006, "requires_plan": "max", "max_mode": True},
}
DEFAULT_MODEL = "sonnet"
MAX_MODE_DELEGATES = ["haiku", "gemini-flash", "gpt-5-mini", "qwen-coder", "opus", "gpt-5"]
OWNER_ID = "984cd1b9-28d9-404a-96d5-449d56e3cee8"


# ═══ RATE LIMITING ═══
def rate_limit(action="api", limit=30, window_ms=60000):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            user = get_user_from_token(token)
            if not user:
                return jsonify({"error": "Unauthorized"}), 401
            allowed, remaining, reset_at = store.check_rate_limit(user["id"], action, limit, window_ms)
            if not allowed:
                resp = jsonify({"error": "Rate limit exceeded", "retry_at": reset_at})
                resp.headers["Retry-After"] = str(max(0, (reset_at - int(time.time() * 1000)) // 1000))
                return resp, 429
            resp = f(*args, **kwargs)
            if hasattr(resp, 'headers'):
                resp.headers["X-RateLimit-Remaining"] = str(remaining)
            return resp
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator


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
    if request.path == "/health":
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
pending_auto_sessions = {}
pending_auto_sessions_lock = Lock()
AUTO_CONNECT_EXPIRY_MS = 2 * 60 * 60 * 1000  # 2 hours
web_heartbeats = {}
web_heartbeats_lock = Lock()
workspace_calls = {}
workspace_calls_lock = Lock()

# ═══ AUTH HELPERS ═══
def hash_password(password):
    if not HAS_BCRYPT:
        return password
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def _is_bcrypt_hash(s):
    return isinstance(s, str) and len(s) >= 59 and s.startswith(("$2a$", "$2b$", "$2y$"))

def fetch_roblox_profile(roblox_id):
    rid = str(roblox_id).strip()
    if not rid:
        return None
    try:
        with urlopen(f"https://users.roblox.com/v1/users/{rid}", timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        username = data.get("name") or data.get("displayName") or ""
        display_name = data.get("displayName") or username
        try:
            with urlopen(
                f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={rid}&size=150x150&format=Png&isCircular=true",
                timeout=6,
            ) as resp:
                thumb = json.loads(resp.read().decode("utf-8"))
            image = ""
            thumb_data = thumb.get("data") or []
            if thumb_data:
                image = thumb_data[0].get("imageUrl") or ""
        except Exception:
            image = ""
        return {
            "roblox_id": rid,
            "username": username,
            "display_name": display_name,
            "avatar_url": image,
        }
    except Exception:
        return None

def verify_password(password, hashed):
    if not isinstance(hashed, str) or not hashed:
        return False
    if not HAS_BCRYPT:
        return password == hashed
    if _is_bcrypt_hash(hashed):
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception:
            return False
    # Legacy / pre-bcrypt accounts stored the password as-is in `password_hash`.
    # Accept the plaintext match so imported data still works.
    return password == hashed

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
    with pending_auto_sessions_lock:
        expired_auto = [k for k, v in pending_auto_sessions.items() if now - v["created_at"] > AUTO_CONNECT_EXPIRY_MS]
        for k in expired_auto:
            del pending_auto_sessions[k]

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

MAX_HISTORY_TURNS = 8  # keep last N user+assistant turns verbatim
MAX_MSG_CHARS = 8000   # truncate very long single messages


def _compact_history(history):
    """Trim chat history sent to the AI to control cost.
    Strategy: keep the last MAX_HISTORY_TURNS turns (user+assistant pairs)
    verbatim. Everything older is replaced with a single short summary note
    so the AI still has continuity without re-sending (and re-paying for)
    every old message every turn. Long individual messages are truncated.
    """
    if not history:
        return [], 0
    # Walk backwards collecting up to N user-role anchors.
    keep_idx = 0
    user_seen = 0
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if isinstance(m, dict) and m.get("role") == "user":
            user_seen += 1
            if user_seen >= MAX_HISTORY_TURNS:
                keep_idx = i
                break
    older = history[:keep_idx]
    recent = history[keep_idx:]
    trimmed_older_count = len(older)

    out = []
    if older:
        # Compact summary stub — does not call the AI, just lists topics
        # cheaply from user turns so the model has continuity context.
        topics = []
        for m in older:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    topics.append(c.strip().split("\n")[0][:80])
        topic_blurb = " · ".join(topics[-6:]) if topics else "earlier discussion"
        out.append({
            "role": "user",
            "content": f"[Earlier in this conversation ({trimmed_older_count} messages): {topic_blurb}]",
        })
        out.append({
            "role": "assistant",
            "content": "Got it, continuing from where we left off.",
        })

    # Truncate any oversized individual message
    for m in recent:
        if isinstance(m, dict) and isinstance(m.get("content"), str) and len(m["content"]) > MAX_MSG_CHARS:
            mc = dict(m)
            mc["content"] = m["content"][:MAX_MSG_CHARS] + "\n\n... [truncated to control cost]"
            out.append(mc)
        else:
            out.append(m)
    return out, trimmed_older_count


def _memory_preface(conv_id):
    """Return a short preface that injects saved project facts for this conversation."""
    if not conv_id:
        return ""
    mems = store.list_memories(conv_id)
    if not mems:
        return ""
    bullets = "\n".join(f"- {m.get('text','')}" for m in mems if isinstance(m, dict))
    return (
        "Project memory (facts the user has asked you to remember about this project):\n"
        f"{bullets}\n\n"
        "Use these facts when relevant. Do NOT re-ask. Use the `remember` tool to add new facts and `forget` to remove.\n\n"
    )


def build_chat_messages(session, user_message, context, conv_id=None):
    history, trimmed = _compact_history(list(session["conversation"]))
    session["history_trimmed_count"] = trimmed
    messages = history
    preface = _memory_preface(conv_id)
    ctx_msg = f"{preface}User message:\n{user_message}\n\nCurrent script name:\n{context.get('current_script_name')}\n\nSelected instance:\n{json.dumps(context.get('selected_instance'), indent=2)}"
    if messages and messages[-1].get("role") == "user" and isinstance(messages[-1].get("content"), str) and messages[-1].get("content") == user_message:
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
    - You have direct access to tools for reading, writing, creating, and deleting scripts, searching code, inspecting and modifying the instance tree, and adding new instances.
    - CRITICAL: You may only call ONE tool per response. Never call multiple tools at once. Call one tool, wait for the result, then decide your next step.
    - Always use list_scripts or get_script_tree first before trying to read a specific script by name.
    - Prefer get_script_tree over get_instance_tree — get_instance_tree returns extremely large data. Use find_instance or get_script_tree instead.
    - get_output_log and get_error_log may return empty results — tell the user to check the Studio Output window directly if needed.
    - If a tool result is truncated, use more specific tools like read_script or find_instance.

    INSTANCE OPERATIONS:
    - get_properties(path): Read all properties of any part, folder, tool, or other instance. Use this to inspect Position, Size, Transparency, CanCollide, Anchored, Color, Material, etc.
    - set_property(path, property, value): Change any writable property on any instance. Use this to move objects (Position/CFrame), rename (Name), make transparent (Transparency=1), disable collision (CanCollide=false), anchor (Anchored=true), change color/material, reparent (Parent), etc. Call once per property change.
    - add_instance(class_name, parent_path, name, properties?): Create a new Roblox Instance under a parent. Supports any class: Folder, Part, Tool, SpawnLocation, MeshPart, PointLight, Sound, Decal, etc. Use the optional properties dict to set initial values like Transparency, CanCollide, Anchored, Position, Size, Material, Color.
    - To build complex structures, chain add_instance calls. Example workflow:
      1. add_instance("Folder", "Workspace", "MyWeapon")
      2. add_instance("Tool", "Workspace.MyWeapon", "Sword")
      3. add_instance("Part", "Workspace.MyWeapon.Sword", "Handle", {"Transparency": 1, "CanCollide": false, "Anchored": false, "Size": {1, 1, 4}})
      4. add_instance("Script", "Workspace.MyWeapon.Sword", "SwordScript")
    - Use find_instance or get_properties to verify your changes after making them.
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
    {"name": "get_instance_tree", "description": "Get the Explorer instance tree. WARNING: Returns very large data.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_properties", "description": "Get all properties of an instance by its path in the Explorer tree. Returns name, class, position, size, color, transparency, collision, and all other readable properties. Use find_instance first if you don't know the exact path.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string", "description": "Full path in the Explorer, e.g. 'Workspace.PlayerController' or 'Workspace.Map.Folder1.Part5'"}}, "required": ["instance_path"]}},
    {"name": "set_property", "description": "Set a property on any instance by path. Can change Name, Position, Size, Transparency, CanCollide, Anchored, Color, Material, Parent, CFrame, and any other writable property. Use this to move objects, rename them, make them transparent, disable collisions, etc. You may set multiple properties by calling this tool once per property.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string", "description": "Full path, e.g. 'Workspace.Map.Wall'"}, "property": {"type": "string", "description": "Property name, e.g. 'Name', 'Transparency', 'CanCollide', 'Position', 'Anchored', 'Parent'"}, "value": {"description": "The new value. Strings for Name/Parent, numbers for Transparency/Size, booleans for CanCollide/Anchored, tables for Position/Color (e.g. {0, 5, 0})"}}, "required": ["instance_path", "property", "value"]}},
    {"name": "add_instance", "description": "Create a new Roblox Instance (Folder, Part, Tool, SpawnLocation, MeshPart, PointLight, Script, LocalScript, ModuleScript, Sound, Decal, BillboardGui, Attachment, etc.) under a parent path. Optionally set initial properties like Name, Transparency, CanCollide, Anchored, Position, Size, Color, Material, and more. This is how you build structures — e.g. add a Folder, then add a Tool inside it, then add a Part named Handle inside the Tool with CanCollide=false and Transparency=1.", "input_schema": {"type": "object", "properties": {"class_name": {"type": "string", "description": "The Roblox class to create, e.g. 'Folder', 'Part', 'Tool', 'SpawnLocation', 'MeshPart', 'PointLight', 'Sound', 'Decal', 'BillboardGui'"}, "parent_path": {"type": "string", "description": "Full path of the parent instance, e.g. 'Workspace', 'Workspace.Map', 'StarterPlayer.StarterPlayerScripts'"}, "name": {"type": "string", "description": "Name for the new instance"}, "properties": {"type": "object", "description": "Optional initial properties to set on the new instance, e.g. {\"Transparency\": 1, \"CanCollide\": false, \"Anchored\": true, \"Position\": {0, 10, 0}, \"Size\": {4, 1, 4}, \"Material\": \"SmoothPlastic\", \"Color\": {255, 0, 0}}", "additionalProperties": {}}}, "required": ["class_name", "parent_path", "name"]}},
    {"name": "find_instance", "description": "Find an instance anywhere in the game by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_selection", "description": "Return the current Explorer selection.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_current_script", "description": "Return the currently selected or active script name and source if available.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_place_metadata", "description": "Return game name, place id, and version.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_workspace_summary", "description": "Return a compact snapshot of the connected place: top-level Roblox services (Workspace, ReplicatedStorage, ServerScriptService, StarterGui, etc.) with each service's child count, descendant count, script count broken down by Script/LocalScript/ModuleScript, and up to 12 example top-level children. Cheap and safe — call this as the FIRST step of any task to understand the user's place before reading scripts or modifying instances.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "snapshot_script", "description": "Save a snapshot of a script before modification.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "diff_script", "description": "Show differences against the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "restore_script", "description": "Restore a script to the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "create_checkpoint", "description": "Save a named checkpoint of one or more scripts.", "input_schema": {"type": "object", "properties": {"label": {"type": "string"}, "scripts": {"type": "object", "additionalProperties": {"type": "string"}}}, "required": ["label", "scripts"]}},
    {"name": "list_checkpoints", "description": "List all checkpoints saved for the current session.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "restore_checkpoint", "description": "Retrieve saved script contents from a checkpoint by ID.", "input_schema": {"type": "object", "properties": {"checkpoint_id": {"type": "string"}}, "required": ["checkpoint_id"]}},
    {"name": "remember", "description": "Save a short fact about this project so you remember it in future turns without re-asking the user. Use for preferences (e.g. 'uses Knit framework'), conventions ('all RemoteEvents live in ReplicatedStorage.Net'), or recurring requirements. One fact per call. Do NOT save secrets.", "input_schema": {"type": "object", "properties": {"fact": {"type": "string", "description": "A single concise fact, max 500 chars."}}, "required": ["fact"]}},
    {"name": "forget", "description": "Remove a previously saved project memory by its id (the id shown in the Project memory list).", "input_schema": {"type": "object", "properties": {"memory_id": {"type": "string"}}, "required": ["memory_id"]}},
]

SERVER_SIDE_TOOLS = {"create_checkpoint", "list_checkpoints", "restore_checkpoint", "remember", "forget"}

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
            [{"id": c["id"], "label": c["label"], "created_at": c["created_at"], "scripts_count": len(c.get("scripts", {}))}
             for c in ckpts.values() if not sid or c.get("session_id") == sid],
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
        return {"ok": True, "checkpoint_id": checkpoint_id, "label": ckpt["label"], "scripts": scripts,
                "message": f"Checkpoint has {len(scripts)} script(s). Use write_script for each to restore."}
    elif name == "remember":
        conv_id = session.get("conv_id")
        fact = (args.get("fact") or "").strip()
        if not conv_id:
            return {"error": "No active conversation — cannot save memory."}
        if not fact:
            return {"error": "fact is required"}
        item = store.add_memory(conv_id, fact, source="ai")
        if not item:
            return {"error": "Could not save memory"}
        return {"ok": True, "id": item["id"], "text": item["text"]}
    elif name == "forget":
        conv_id = session.get("conv_id")
        mem_id = (args.get("memory_id") or "").strip()
        if not conv_id or not mem_id:
            return {"error": "memory_id and active conversation required"}
        ok = store.delete_memory(conv_id, mem_id)
        return {"ok": ok}
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


# ═══ WEBHOOK HELPER ═══
EVENT_COLORS = {
    "user_registered": 0x57F287,
    "user_blocked": 0xED4245,
    "user_deleted": 0xFEE75C,
    "credits_changed": 0x5865F2,
    "maintenance_toggled": 0xEB459E,
    "bulk_grant_credits": 0x5865F2,
    "test": 0x95A5A6,
}

def _build_discord_payload(event_type, payload):
    color = EVENT_COLORS.get(event_type, 0x95A5A6)
    fields = [{"name": k, "value": str(v)[:1024], "inline": True} for k, v in payload.items() if v]
    import datetime
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "username": "Rux",
        "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        "embeds": [{
            "title": f"Event: `{event_type}`",
            "color": color,
            "fields": fields,
            "timestamp": ts,
            "footer": {"text": "Rux Admin"}
        }]
    }

def _resolve_webhook_url(url):
    """Resolve env var references like $VAR_NAME or bare VAR_NAME (no http)."""
    if not url:
        return url
    import os
    if url.startswith("$"):
        url = os.environ.get(url[1:], url)
    elif not url.startswith("http"):
        resolved = os.environ.get(url, "")
        if resolved:
            url = resolved
    return url

def _fire_webhook(event_type, payload):
    """Fire webhook if configured."""
    try:
        webhooks = store.get_webhooks()
        url = _resolve_webhook_url(webhooks.get("url", ""))
        if not url or not url.startswith("http"):
            return
        import urllib.request
        is_discord = "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url
        if is_discord:
            body = json.dumps(_build_discord_payload(event_type, payload)).encode()
        else:
            body = json.dumps({"event": event_type, "payload": payload, "timestamp": int(time.time() * 1000)}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "RuxBot/1.0"
        }, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Webhook failures should never break the request


# ═══ HEALTH CHECK ═══
@app.route("/health", methods=["GET"])
def health_check():
    now = time.time()
    with plugin_registry_lock:
        active_plugins = sum(1 for p in plugin_registry.values() if now - p["last_seen"] < PLUGIN_OFFLINE_AFTER)
    return jsonify({"status": "ok", "timestamp": int(time.time()), "active_plugins": active_plugins, "maintenance": store.is_maintenance()})


# ═══════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════

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
    reason = data.get("reason", "Admin credit adjustment")
    if not user_id or balance is None or max_credit is None:
        return jsonify({"error": "Missing fields"}), 400
    store.set_user_credits(user_id, balance, max_credit)
    store.save_credit_entry(user_id, float(balance) - float(store.get_credits(user_id)[0]), reason, request.user.get("id"))
    store.save_audit_entry(request.user.get("id", ""), "set_credits", user_id, f"balance={balance} max={max_credit} reason={reason}")
    _fire_webhook("credits_changed", {"user_id": user_id, "balance": balance, "admin_id": request.user.get("id")})
    return jsonify({"ok": True})

@app.route("/admin/api/credits/grant", methods=["POST"])
@require_admin
def admin_grant_credits():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    amount = data.get("amount")
    reason = data.get("reason", "Admin grant")
    if not user_id or amount is None:
        return jsonify({"error": "Missing fields"}), 400
    amount = float(amount)
    balance, last_updated = store.get_credits(user_id)
    max_credit = float(store.get_all_users_with_credits()[0].get("max_credit", store.MAX_CREDITS)) if store.get_user_by_id(user_id) else store.MAX_CREDITS
    users_list = store.get_all_users_with_credits()
    for u in users_list:
        if u["id"] == user_id:
            max_credit = u.get("max_credit", store.MAX_CREDITS)
            break
    new_balance = round(min(balance + amount, max_credit), 6)
    store.set_user_credits(user_id, new_balance, max_credit)
    store.save_credit_entry(user_id, amount, reason, request.user.get("id"))
    store.save_audit_entry(request.user.get("id", ""), "grant_credits", user_id, f"amount=+{amount} reason={reason}")
    return jsonify({"ok": True, "new_balance": new_balance})

@app.route("/admin/api/credits/history", methods=["GET"])
@require_admin
def admin_credit_history():
    limit = int(request.args.get("limit", 100))
    history = store.get_global_credit_history(limit)
    return jsonify(history)

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
    store.save_audit_entry(request.user.get("id", ""), "block_user", user_id)
    _fire_webhook("user_blocked", {"user_id": user_id, "admin_id": request.user.get("id")})
    return jsonify({"ok": True})

@app.route("/admin/api/unblock", methods=["POST"])
@require_admin
def admin_unblock_user():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    store.unblock_user(user_id)
    store.save_audit_entry(request.user.get("id", ""), "unblock_user", user_id)
    return jsonify({"ok": True})

@app.route("/admin/api/promote", methods=["POST"])
@require_owner
def admin_promote_user():
    data = request.get_json(force=True)
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    store.add_admin(user_id)
    store.save_audit_entry(request.user.get("id", ""), "promote_user", user_id)
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
    store.save_audit_entry(request.user.get("id", ""), "demote_user", user_id)
    return jsonify({"ok": True})

@app.route("/admin/api/user/<user_id>", methods=["DELETE"])
@require_owner
def admin_delete_user(user_id):
    if user_id == OWNER_ID:
        return jsonify({"error": "Cannot delete owner"}), 403
    if not store.delete_user(user_id):
        return jsonify({"error": "Failed to delete"}), 400
    store.save_audit_entry(request.user.get("id", ""), "delete_user", user_id)
    _fire_webhook("user_deleted", {"user_id": user_id})
    return jsonify({"ok": True})

@app.route("/admin/api/user/<user_id>/detail", methods=["GET"])
@require_admin
def admin_user_detail(user_id):
    detail = store.get_user_detail(user_id)
    if not detail:
        return jsonify({"error": "User not found"}), 404
    return jsonify(detail)

@app.route("/admin/api/user/<user_id>/note", methods=["POST"])
@require_admin
def admin_set_user_note(user_id):
    data = request.get_json(force=True)
    note = data.get("note", "")
    store.set_user_note(user_id, note)
    store.save_audit_entry(request.user.get("id", ""), "set_note", user_id, f"note_length={len(note)}")
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
    store.save_audit_entry(request.user.get("id", ""), "toggle_maintenance", "", f"enabled={enabled}")
    _fire_webhook("maintenance_toggled", {"enabled": enabled, "admin_id": request.user.get("id")})
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
    store.save_audit_entry(request.user.get("id", ""), "import_data", "", f"keys={list(data.keys())}")
    return jsonify({"ok": True})

# ═══ ADMIN: STATS ═══
@app.route("/admin/api/stats", methods=["GET"])
@require_admin
def admin_stats():
    stats = store.get_system_stats()
    return jsonify(stats)

# ═══ ADMIN: AUDIT LOG ═══
@app.route("/admin/api/audit-log", methods=["GET"])
@require_admin
def admin_audit_log():
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))
    action_filter = request.args.get("action")
    log = store.get_audit_log(limit, offset, action_filter)
    return jsonify(log)

# ═══ ADMIN: SESSIONS ═══
@app.route("/admin/api/sessions", methods=["GET"])
@require_admin
def admin_get_sessions():
    sessions = store.get_all_sessions()
    return jsonify(sessions)

@app.route("/admin/api/sessions/<path:token_prefix>", methods=["DELETE"])
@require_admin
def admin_force_logout(token_prefix):
    all_sessions = store.get_all_sessions()
    for s in all_sessions:
        if s["token_full"].startswith(token_prefix[:8]):
            store.delete_session(s["token_full"])
            store.save_audit_entry(request.user.get("id", ""), "force_logout", s["user_id"], f"token={s['token_preview']}")
            return jsonify({"ok": True})
    return jsonify({"error": "Session not found"}), 404

@app.route("/admin/api/sessions/user/<user_id>", methods=["DELETE"])
@require_admin
def admin_logout_all_user(user_id):
    count = store.delete_all_user_sessions(user_id)
    store.save_audit_entry(request.user.get("id", ""), "logout_all", user_id, f"count={count}")
    return jsonify({"ok": True, "count": count})

# ═══ ADMIN: BULK OPERATIONS ═══
@app.route("/admin/api/bulk/block", methods=["POST"])
@require_admin
def admin_bulk_block():
    data = request.get_json(force=True)
    user_ids = data.get("user_ids", [])
    if not user_ids:
        return jsonify({"error": "Missing user_ids"}), 400
    blocked = []
    for uid in user_ids:
        if uid == OWNER_ID:
            continue
        if store.is_admin(uid) and request.user.get("id") != OWNER_ID:
            continue
        store.block_user(uid)
        blocked.append(uid)
    store.save_audit_entry(request.user.get("id", ""), "bulk_block", "", f"count={len(blocked)} ids={blocked[:5]}")
    return jsonify({"ok": True, "blocked": blocked})

@app.route("/admin/api/bulk/unblock", methods=["POST"])
@require_admin
def admin_bulk_unblock():
    data = request.get_json(force=True)
    user_ids = data.get("user_ids", [])
    if not user_ids:
        return jsonify({"error": "Missing user_ids"}), 400
    for uid in user_ids:
        store.unblock_user(uid)
    store.save_audit_entry(request.user.get("id", ""), "bulk_unblock", "", f"count={len(user_ids)}")
    return jsonify({"ok": True})

@app.route("/admin/api/bulk/credits", methods=["POST"])
@require_admin
def admin_bulk_credits():
    data = request.get_json(force=True)
    user_ids = data.get("user_ids", [])
    amount = data.get("amount")
    reason = data.get("reason", "Bulk credit grant")
    if not user_ids or amount is None:
        return jsonify({"error": "Missing fields"}), 400
    amount = float(amount)
    granted = []
    users_list = store.get_all_users_with_credits()
    for uid in user_ids:
        balance = 0
        max_credit = store.MAX_CREDITS
        for u in users_list:
            if u["id"] == uid:
                balance = u["balance"]
                max_credit = u.get("max_credit", store.MAX_CREDITS)
                break
        new_balance = round(min(balance + amount, max_credit), 6)
        store.set_user_credits(uid, new_balance, max_credit)
        store.save_credit_entry(uid, amount, reason, request.user.get("id"))
        granted.append(uid)
    store.save_audit_entry(request.user.get("id", ""), "bulk_grant_credits", "", f"amount=+{amount} count={len(granted)}")
    return jsonify({"ok": True, "granted": granted})

@app.route("/admin/api/bulk/delete", methods=["POST"])
@require_owner
def admin_bulk_delete():
    data = request.get_json(force=True)
    user_ids = data.get("user_ids", [])
    if not user_ids:
        return jsonify({"error": "Missing user_ids"}), 400
    deleted = []
    for uid in user_ids:
        if uid == OWNER_ID:
            continue
        if store.delete_user(uid):
            deleted.append(uid)
    store.save_audit_entry(request.user.get("id", ""), "bulk_delete", "", f"count={len(deleted)}")
    return jsonify({"ok": True, "deleted": deleted})

# ═══ ADMIN: ANNOUNCEMENTS ═══
@app.route("/admin/api/announcements", methods=["GET"])
@require_admin
def admin_get_announcements():
    anns = store.get_announcements()
    return jsonify(anns)

@app.route("/admin/api/announcements", methods=["POST"])
@require_admin
def admin_create_announcement():
    data = request.get_json(force=True)
    title = data.get("title", "")
    body = data.get("body", "")
    ann_type = data.get("type", "info")
    expires_at = data.get("expires_at", 0)
    if not title:
        return jsonify({"error": "Missing title"}), 400
    ann_data = {
        "id": "ann-" + str(uuid.uuid4()),
        "title": title,
        "body": body,
        "type": ann_type,
        "enabled": True,
        "expires_at": expires_at,
        "created_at": int(time.time() * 1000),
        "admin_id": request.user.get("id", ""),
    }
    store.save_announcement(ann_data)
    store.save_audit_entry(request.user.get("id", ""), "create_announcement", "", f"title={title}")
    return jsonify({"ok": True, "id": ann_data["id"]})

@app.route("/admin/api/announcements/<ann_id>", methods=["PATCH"])
@require_admin
def admin_update_announcement(ann_id):
    data = request.get_json(force=True)
    store.update_announcement(ann_id, data)
    store.save_audit_entry(request.user.get("id", ""), "update_announcement", "", f"id={ann_id}")
    return jsonify({"ok": True})

@app.route("/admin/api/announcements/<ann_id>", methods=["DELETE"])
@require_admin
def admin_delete_announcement(ann_id):
    store.delete_announcement(ann_id)
    store.save_audit_entry(request.user.get("id", ""), "delete_announcement", "", f"id={ann_id}")
    return jsonify({"ok": True})

# ═══ ADMIN: EMAIL BANS ═══
@app.route("/admin/api/email-bans", methods=["GET"])
@require_admin
def admin_get_email_bans():
    bans = store.get_email_bans()
    return jsonify(list(bans.values()))

@app.route("/admin/api/email-bans", methods=["POST"])
@require_admin
def admin_add_email_ban():
    data = request.get_json(force=True)
    pattern = data.get("pattern", "")
    reason = data.get("reason", "")
    if not pattern:
        return jsonify({"error": "Missing pattern"}), 400
    ban_id = store.add_email_ban(pattern, request.user.get("id", ""), reason)
    store.save_audit_entry(request.user.get("id", ""), "add_email_ban", "", f"pattern={pattern}")
    return jsonify({"ok": True, "id": ban_id})

@app.route("/admin/api/email-bans/<ban_id>", methods=["DELETE"])
@require_admin
def admin_delete_email_ban(ban_id):
    store.delete_email_ban(ban_id)
    store.save_audit_entry(request.user.get("id", ""), "delete_email_ban", "", f"id={ban_id}")
    return jsonify({"ok": True})

# ═══ ADMIN: INVITES ═══
@app.route("/admin/api/invites", methods=["GET"])
@require_admin
def admin_get_invites():
    invites = store.get_invites()
    return jsonify(list(invites.values()))

@app.route("/admin/api/invites", methods=["POST"])
@require_admin
def admin_create_invite():
    data = request.get_json(force=True)
    code = data.get("code") or secrets.token_urlsafe(8).upper()[:8]
    max_uses = data.get("max_uses", 1)
    expires_hours = data.get("expires_hours", 0)
    expires_at = int((time.time() + expires_hours * 3600) * 1000) if expires_hours else 0
    store.create_invite(code, max_uses, expires_at, request.user.get("id", ""))
    store.save_audit_entry(request.user.get("id", ""), "create_invite", "", f"code={code} max_uses={max_uses}")
    return jsonify({"ok": True, "code": code})

@app.route("/admin/api/invites/<code>", methods=["DELETE"])
@require_admin
def admin_delete_invite(code):
    store.delete_invite(code)
    store.save_audit_entry(request.user.get("id", ""), "delete_invite", "", f"code={code}")
    return jsonify({"ok": True})

# ═══ ADMIN: SYSTEM CONFIG ═══
@app.route("/admin/api/config", methods=["GET"])
@require_admin
def admin_get_config():
    config = store.get_config()
    return jsonify(config)

@app.route("/admin/api/config", methods=["POST"])
@require_owner
def admin_save_config():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400
    config = store.save_config(data)
    store.save_audit_entry(request.user.get("id", ""), "update_config", "", f"keys={list(data.keys())}")
    return jsonify({"ok": True, "config": config})

# ═══ ADMIN: CONVERSATIONS (MODERATION) ═══
@app.route("/admin/api/conversations", methods=["GET"])
@require_admin
def admin_get_conversations():
    limit = int(request.args.get("limit", 200))
    search = request.args.get("search", "").lower()
    convs = store.get_all_conversations(limit)
    if search:
        convs = [c for c in convs if search in c.get("title", "").lower() or search in c.get("user_id", "")]
    return jsonify(convs)

@app.route("/admin/api/conversations/<conv_id>", methods=["GET"])
@require_admin
def admin_get_conversation(conv_id):
    conv = store.get_conv(conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    return jsonify(conv)

@app.route("/admin/api/conversations/<conv_id>/flag", methods=["POST"])
@require_admin
def admin_flag_conversation(conv_id):
    data = request.get_json(force=True)
    reason = data.get("reason", "Flagged by admin")
    store.flag_conversation(conv_id, reason, request.user.get("id", ""))
    store.save_audit_entry(request.user.get("id", ""), "flag_conversation", "", f"conv_id={conv_id} reason={reason}")
    return jsonify({"ok": True})

# ═══ ADMIN: CHECKPOINTS ═══
@app.route("/admin/api/checkpoints", methods=["GET"])
@require_admin
def admin_get_checkpoints():
    limit = int(request.args.get("limit", 200))
    ckpts = store.get_all_checkpoints(limit)
    return jsonify(ckpts)

# ═══ ADMIN: PLUGIN STATUS ═══
@app.route("/admin/api/plugin-status", methods=["GET"])
@require_admin
def admin_plugin_status():
    now = time.time()
    with plugin_registry_lock:
        result = []
        for pid, info in plugin_registry.items():
            result.append({
                "plugin_id": pid,
                "session_id": info.get("session_id", ""),
                "user_id": info.get("user_id", ""),
                "last_seen": info.get("last_seen", 0),
                "status": info.get("status", ""),
                "active": (now - info.get("last_seen", 0)) < PLUGIN_OFFLINE_AFTER,
            })
    return jsonify(result)

# ═══ ADMIN: WEBHOOKS ═══
@app.route("/admin/api/webhooks", methods=["GET"])
@require_admin
def admin_get_webhooks():
    return jsonify(store.get_webhooks())

@app.route("/admin/api/webhooks", methods=["POST"])
@require_owner
def admin_save_webhooks():
    data = request.get_json(force=True)
    store.save_webhooks(data)
    store.save_audit_entry(request.user.get("id", ""), "update_webhooks", "", f"url={data.get('url', '')[:30]}")
    return jsonify({"ok": True})

@app.route("/admin/api/webhooks/test", methods=["POST"])
@require_admin
def admin_test_webhook():
    webhooks = store.get_webhooks()
    url = _resolve_webhook_url(webhooks.get("url", ""))
    if not url or not url.startswith("http"):
        return jsonify({"error": "No webhook URL configured"}), 400
    try:
        import urllib.request, urllib.error
        is_discord = "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url
        if is_discord:
            body = json.dumps(_build_discord_payload("test", {"message": "Test from Rux Admin"})).encode()
        else:
            body = json.dumps({"event": "test", "payload": {"message": "Test from Rux Admin"}, "timestamp": int(time.time() * 1000)}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "RuxBot/1.0"
        }, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return jsonify({"ok": True, "status": resp.status})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            return jsonify({"ok": False, "error": f"HTTP {e.code}: {detail}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]})

@app.route("/admin")
def admin_page():
    return render_template("admin.html")


# ═══════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    roblox_id = data.get("roblox_id") or ""
    invite_code = data.get("invite_code") or ""

    if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Invalid email"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    if store.check_email_banned(email):
        return jsonify({"error": "Registration not allowed for this email"}), 403

    config = store.get_config()
    if config.get("invite_only", False):
        if not invite_code:
            return jsonify({"error": "Invite code required"}), 400
        ok, msg = store.use_invite(invite_code)
        if not ok:
            return jsonify({"error": msg}), 400

    existing = store.get_user_by_email(email)
    if existing:
        return jsonify({"error": "Email already registered"}), 400

    user_id = str(uuid.uuid4())
    store.save_user(user_id, email, hash_password(password), roblox_id)

    starting = config.get("default_starting_credits", store.MAX_CREDITS)
    store.init_credits(user_id)
    if starting != store.MAX_CREDITS:
        store.set_user_credits(user_id, starting, config.get("max_credits_default", store.MAX_CREDITS))

    token = str(uuid.uuid4())
    store.save_session(token, user_id)

    _fire_webhook("user_registered", {"user_id": user_id, "email": email})

    return jsonify({"token": token, "user": {"id": user_id, "email": email, "roblox_id": roblox_id}})

@app.route("/auth/roblox-profile", methods=["POST"])
def auth_roblox_profile():
    data = request.get_json(force=True)
    roblox_id = (data.get("roblox_id") or "").strip()
    profile = fetch_roblox_profile(roblox_id)
    if not profile:
        return jsonify({"error": "Roblox profile not found"}), 404
    return jsonify(profile)

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

    if user.get("blocked"):
        return jsonify({"error": "Account blocked", "blocked": True}), 403

    # Auto-upgrade legacy plaintext password storage to a real bcrypt hash.
    if HAS_BCRYPT and not _is_bcrypt_hash(user.get("password_hash") or ""):
        try:
            store.update_user_password(user["id"], hash_password(password))
        except Exception:
            pass

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
            if pid and pid in plugin_registry and now - plugin_registry[pid]["last_seen"] < PLUGIN_OFFLINE_AFTER:
                session_id = active_session.get("session_id")

    update_web_heartbeat(user["id"])

    prefs = store.get_preferences(user["id"])
    announcements = store.get_active_announcements()

    return jsonify({
        "user": {"id": user["id"], "email": user["email"], "roblox_id": user.get("roblox_id", "")},
        "credits": {"balance": round(balance, 2), "max": store.MAX_CREDITS, "next_credit_at": next_at},
        "session_id": session_id,
        "preferences": prefs,
        "announcements": announcements,
        "plan": store.get_user_plan(user["id"]),
        "spending_cap": store.get_spending_cap(user["id"]),
        "spent_today": round(store.get_daily_spend(user["id"]), 4),
    })

@app.route("/auth/logout", methods=["POST"])
@require_auth
def auth_logout():
    user = request.user
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    store.delete_session(token)
    clear_web_heartbeat(user["id"])
    return jsonify({"ok": True})

@app.route("/auth/change-password", methods=["POST"])
@require_auth
def auth_change_password():
    user = request.user
    data = request.get_json(force=True)
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    if not current_password or not new_password:
        return jsonify({"error": "Both current and new password are required"}), 400
    if len(new_password) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400
    fresh_user = store.get_user_by_id(user["id"])
    if not fresh_user:
        return jsonify({"error": "User not found"}), 404
    if not verify_password(current_password, fresh_user["password_hash"]):
        return jsonify({"error": "Current password is incorrect"}), 401
    new_hash = hash_password(new_password)
    store.update_user_password(user["id"], new_hash)
    current_token = request.headers.get("Authorization", "").replace("Bearer ", "")
    store.delete_all_user_sessions(user["id"])
    store.save_session(current_token, user["id"])
    return jsonify({"ok": True, "message": "Password changed successfully"})

@app.route("/auth/delete-account", methods=["POST"])
@require_auth
def auth_delete_account():
    user = request.user
    data = request.get_json(force=True)
    password = data.get("password", "")
    if not password:
        return jsonify({"error": "Password confirmation required"}), 400
    if user["id"] == OWNER_ID:
        return jsonify({"error": "Cannot delete owner account"}), 403
    fresh_user = store.get_user_by_id(user["id"])
    if not fresh_user:
        return jsonify({"error": "User not found"}), 404
    if not verify_password(password, fresh_user["password_hash"]):
        return jsonify({"error": "Incorrect password"}), 401
    store.self_delete_account(user["id"])
    return jsonify({"ok": True, "message": "Account deleted successfully"})

@app.route("/auth/update-roblox-id", methods=["POST"])
@require_auth
def auth_update_roblox_id():
    user = request.user
    data = request.get_json(force=True)
    roblox_id = data.get("roblox_id", "")
    store.update_user_roblox_id(user["id"], roblox_id)
    return jsonify({"ok": True})

# ═══ ROBLOX GAME ANALYSIS ═══
def _extract_place_id(value):
    """Accept a raw place id, a roblox.com/games/<id> url, or a games/<id> path."""
    s = str(value or "").strip()
    if not s:
        return None
    if s.isdigit():
        return s
    import re
    m = re.search(r"/games/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"placeId=(\d+)", s)
    if m:
        return m.group(1)
    digits = re.search(r"(\d{6,})", s)
    if digits:
        return digits.group(1)
    return None


def _http_json(url, timeout=8):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Rux/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _roblox_universe_icons(universe_ids):
    """Return {universe_id: icon_url} for a list of universe ids."""
    if not universe_ids:
        return {}
    try:
        ids = ",".join(str(u) for u in universe_ids if u)
        ic = _http_json(
            f"https://thumbnails.roblox.com/v1/games/icons?universeIds={ids}"
            f"&size=150x150&format=Png&isCircular=false"
        )
        out = {}
        for row in ic.get("data") or []:
            out[row.get("targetId")] = row.get("imageUrl") or ""
        return out
    except Exception:
        return {}


@app.route("/api/roblox/search-games", methods=["GET"])
@require_auth
def search_roblox_games():
    """Search Roblox games by name. Free — no credits charged for search."""
    q = (request.args.get("q") or "").strip()
    if not q or len(q) < 2:
        return jsonify({"ok": True, "results": []})
    if len(q) > 80:
        q = q[:80]
    try:
        from urllib.parse import quote
        # Roblox public games-autocomplete endpoint returns suggestions with universeId
        url = f"https://apis.roblox.com/search-api/omni-search?searchQuery={quote(q)}&pageType=games&sessionId=rux"
        try:
            data = _http_json(url, timeout=6)
            search_results = data.get("searchResults") or []
            picked = []
            for block in search_results:
                if (block.get("contentGroupType") or "").lower() != "game":
                    continue
                for c in block.get("contents") or []:
                    uid = c.get("universeId") or c.get("rootPlaceId")
                    if not uid:
                        continue
                    picked.append({
                        "universe_id": c.get("universeId"),
                        "place_id": c.get("rootPlaceId"),
                        "name": c.get("name") or "Untitled",
                        "creator_name": (c.get("creatorName") or "").strip(),
                        "playing": c.get("playerCount", 0),
                        "total_up_votes": c.get("totalUpVotes", 0),
                        "total_down_votes": c.get("totalDownVotes", 0),
                    })
                if len(picked) >= 8:
                    break
        except Exception:
            picked = []

        # Fallback: discovery games-list omni search
        if not picked:
            url2 = (
                f"https://games.roblox.com/v1/games/list?model.keyword={quote(q)}"
                f"&model.maxRows=8&model.startRowIndex=0"
            )
            try:
                d2 = _http_json(url2, timeout=6)
                for c in d2.get("games") or []:
                    picked.append({
                        "universe_id": c.get("universeId"),
                        "place_id": c.get("placeId"),
                        "name": c.get("name") or "Untitled",
                        "creator_name": (c.get("creatorName") or "").strip(),
                        "playing": c.get("playerCount", 0),
                        "total_up_votes": c.get("totalUpVotes", 0),
                        "total_down_votes": c.get("totalDownVotes", 0),
                    })
                    if len(picked) >= 8:
                        break
            except Exception:
                pass

        icons = _roblox_universe_icons([p["universe_id"] for p in picked if p.get("universe_id")])
        for p in picked:
            p["icon_url"] = icons.get(p.get("universe_id"), "")
        return jsonify({"ok": True, "results": picked[:8]})
    except Exception as e:
        logging.exception("search games failed")
        return jsonify({"ok": True, "results": [], "warn": str(e)[:120]})


@app.route("/api/roblox/analyze-game", methods=["POST"])
@require_auth
def analyze_roblox_game():
    user = request.user
    data = request.get_json(force=True) or {}
    raw = data.get("place_id") or data.get("url") or ""
    given_universe = data.get("universe_id")
    place_id = _extract_place_id(raw)
    if not place_id and not given_universe:
        return jsonify({"error": "Couldn't read a place ID or game URL from that input."}), 400

    balance, _ = store.get_credits(user["id"])
    cost = 1.0
    if balance < cost:
        return jsonify({"error": "Not enough credits — analysis costs 1 credit."}), 402

    try:
        if given_universe:
            universe_id = given_universe
        else:
            u = _http_json(f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
            universe_id = u.get("universeId")
        if not universe_id:
            return jsonify({"error": "Couldn't resolve that place into a game."}), 404

        games = _http_json(f"https://games.roblox.com/v1/games?universeIds={universe_id}")
        items = games.get("data") or []
        if not items:
            return jsonify({"error": "No game info found for that place."}), 404
        g = items[0]

        icon_url = ""
        try:
            ic = _http_json(
                f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}"
                f"&size=512x512&format=Png&isCircular=false"
            )
            icd = ic.get("data") or []
            if icd:
                icon_url = icd[0].get("imageUrl") or ""
        except Exception:
            pass

        thumb_url = ""
        try:
            th = _http_json(
                f"https://thumbnails.roblox.com/v1/games/multiget/thumbnails?universeIds={universe_id}"
                f"&countPerUniverse=1&defaults=true&size=768x432&format=Png&isCircular=false"
            )
            thd = th.get("data") or []
            if thd:
                arr = thd[0].get("thumbnails") or []
                if arr:
                    thumb_url = arr[0].get("imageUrl") or ""
        except Exception:
            pass

        creator = g.get("creator") or {}
        if not place_id:
            place_id = str(g.get("rootPlaceId") or "")
        result = {
            "place_id": place_id,
            "universe_id": universe_id,
            "name": g.get("name", "Untitled game"),
            "description": (g.get("description") or "").strip(),
            "creator_name": creator.get("name", ""),
            "creator_type": creator.get("type", ""),
            "playing": g.get("playing", 0),
            "visits": g.get("visits", 0),
            "favorites": g.get("favoritedCount", 0),
            "max_players": g.get("maxPlayers", 0),
            "genre": g.get("genre", ""),
            "created": g.get("created", ""),
            "updated": g.get("updated", ""),
            "icon_url": icon_url,
            "thumbnail_url": thumb_url,
            "play_url": f"https://www.roblox.com/games/{place_id}" if place_id else "",
        }
    except Exception as e:
        logging.exception("analyze game failed")
        return jsonify({"error": f"Couldn't reach Roblox: {e}"}), 502

    new_balance, last_updated = store.deduct_credits(user["id"], cost)
    next_at = last_updated + store.CREDIT_INTERVAL_MS if last_updated > 0 else 0
    return jsonify({
        "ok": True,
        "game": result,
        "credits": {"balance": round(new_balance, 2), "max": store.MAX_CREDITS, "next_credit_at": next_at},
    })


@app.route("/api/roblox/insights", methods=["POST"])
@require_auth
def roblox_game_insights():
    """Have Sonnet analyze a fetched game and return structured recreatable systems."""
    user = request.user
    data = request.get_json(force=True) or {}
    game = data.get("game") or {}
    if not game or not game.get("name"):
        return jsonify({"error": "Game data required."}), 400

    if not anthropic_client:
        return jsonify({"error": "AI provider unavailable right now."}), 503

    prompt = f"""You are a senior Roblox game-design analyst.

Analyse this Roblox game and produce a JSON-only response (no prose outside JSON).

Game name: {game.get('name','')}
Creator: {game.get('creator_name','')}
Genre: {game.get('genre','')}
Concurrent players: {game.get('playing',0)}
Total visits: {game.get('visits',0)}
Favorites: {game.get('favorites',0)}
Description:
\"\"\"
{(game.get('description') or '')[:2500]}
\"\"\"

Return JSON of this exact shape:
{{
  "summary": "2-3 sentence overview of the game's hook",
  "gameplay": "what the player actually does, 3-5 sentences",
  "themes": ["short", "tag", "list"],
  "appeal": "why this game grew, 2 sentences",
  "systems": [
    {{
      "name": "Short name like 'Plot ownership' or 'Shop & economy'",
      "description": "1-2 sentence explanation of the mechanic",
      "scope": "small | medium | large",
      "feasibility": 0-100,
      "luau_difficulty": "easy | medium | hard",
      "key_components": ["bullet", "of", "scripts/services/instances needed"]
    }}
  ]
}}

Pick 4-7 distinct, recreatable systems that capture the core loop. Be honest with feasibility — solo Studio devs may struggle with complex network or asset-heavy systems."""

    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in resp.content:
            if hasattr(block, "text"):
                text += block.text
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return jsonify({"error": "AI returned no JSON."}), 502
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            cleaned = re.sub(r",\s*([}\]])", r"\1", m.group(0))
            parsed = json.loads(cleaned)
        if not isinstance(parsed.get("systems"), list):
            parsed["systems"] = []
        for s in parsed["systems"]:
            try:
                s["feasibility"] = max(0, min(100, int(s.get("feasibility", 50))))
            except Exception:
                s["feasibility"] = 50
        return jsonify({"ok": True, "insights": parsed})
    except Exception as e:
        logging.exception("game insights failed")
        return jsonify({"error": f"AI analysis failed: {e}"}), 502

# ═══ PREFERENCES ROUTES ═══
@app.route("/api/preferences", methods=["GET"])
@require_auth
def get_preferences():
    prefs = store.get_preferences(request.user["id"])
    return jsonify(prefs)

@app.route("/api/preferences", methods=["POST"])
@require_auth
def save_preferences():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400
    prefs = store.save_preferences(request.user["id"], data)
    return jsonify({"ok": True, "preferences": prefs})

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
            if pid and pid in plugin_registry and time.time() - plugin_registry[pid]["last_seen"] < PLUGIN_OFFLINE_AFTER:
                plugin_connected = True
                session_id = active_session.get("session_id")

    balance, _ = store.get_credits(user["id"])
    announcements = store.get_active_announcements()

    return jsonify({
        "ok": True,
        "plugin_connected": plugin_connected,
        "session_id": session_id,
        "credits": round(balance, 2),
        "announcements": announcements,
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

@app.route("/connect/register", methods=["POST"])
@require_auth
def connect_register_session():
    """Register a session for auto-connect via Roblox ID matching."""
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400
    user_id = request.user["id"]
    with pending_auto_sessions_lock:
        pending_auto_sessions[user_id] = {
            "session_id": session_id,
            "created_at": int(time.time() * 1000),
        }
    return jsonify({"ok": True})

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
            # Check for pending auto-connect session registered from /code/ URL
            with pending_auto_sessions_lock:
                pending = pending_auto_sessions.get(user_id)
                if pending and (int(time.time() * 1000) - pending["created_at"]) < AUTO_CONNECT_EXPIRY_MS:
                    session_id = pending["session_id"]
                    method = "auto"
                    del pending_auto_sessions[user_id]

            if not session_id:
                existing = store.get_user_plugin(user_id)
                if existing and existing.get("session_id"):
                    session_id = existing["session_id"]
                    method = "auto_reuse"
                else:
                    session_id = str(uuid.uuid4())
                    method = "auto_new"

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
        return jsonify({"error": "no_credits", "balance": round(balance2, 2), "next_credit_at": next_at}), 403

    data = request.get_json(force=True)
    session_id = data.get("session_id") or str(uuid.uuid4())
    mode = data.get("mode", "chat")
    user_message = data.get("message", "")
    model_key = data.get("model", DEFAULT_MODEL)
    conversation_history = data.get("conversation_history", [])
    conv_id = data.get("conv_id") or None
    context = build_context(data)

    # Plan gate — Max model is paid-only
    mi_check = resolve_model(model_key)
    required_plan = mi_check.get("requires_plan")
    if required_plan:
        user_plan = store.get_user_plan(user["id"])
        rank = {"free": 0, "core": 1, "max": 2}
        if rank.get(user_plan, 0) < rank.get(required_plan, 99):
            return jsonify({
                "error": "plan_required",
                "required_plan": required_plan,
                "current_plan": user_plan,
                "message": f"This model requires the {required_plan.title()} plan.",
            }), 402

    # Daily spending cap gate
    cap = store.get_spending_cap(user["id"])
    if cap > 0:
        today_spent = store.get_daily_spend(user["id"])
        if today_spent >= cap:
            return jsonify({
                "error": "spending_cap_reached",
                "cap": cap, "spent_today": round(today_spent, 4),
                "message": f"Daily cap of {cap:g} credits reached. Raise it in Settings to continue.",
            }), 402

    session = get_session(session_id)
    session["conversation"] = conversation_history
    session["latest_context"] = context
    session["model_key"] = model_key
    session["user_id"] = user["id"]
    session["conv_id"] = conv_id
    session["accumulated_reply"] = ""
    session["accumulated_cost"] = 0.0

    mi = resolve_model(model_key)
    mid, prov, cpt = mi["id"], mi["provider"], mi["credit_per_token"]

    try:
        if mode == "chat":
            msgs = build_chat_messages(session, user_message, context, conv_id=conv_id)
            if mi.get("max_mode") and msgs and isinstance(msgs[-1].get("content"), str):
                msgs[-1]["content"] = (
                    "[MAX MODE] You are operating with the highest tier. Think deeply, "
                    "use tools liberally, save important facts with `remember`, and produce "
                    "a thorough, production-ready answer.\n\n" + msgs[-1]["content"]
                )
            if prov == "anthropic":
                r = call_anthropic(mid, msgs, tools=TOOL_DEFINITIONS)
                output_tokens = r.usage.output_tokens
                cost = round(output_tokens * cpt, 6)
                balance, _ = store.deduct_credits(user["id"], cost)
                store.save_credit_entry(user["id"], -cost, f"AI chat ({mi['label']})")

                first_tc, all_tcs, reply_text = extract_tool_info(r.content)

                if first_tc:
                    assistant_content = build_assistant_content(r.content, first_tc["id"])
                    if len(all_tcs) > 1:
                        print(f"[Rux] WARNING: {len(all_tcs)} tool_use blocks, executing first: {first_tc['name']}")
                        reply_text += f"\n\n[Calling {len(all_tcs)} tools one at a time.]"

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
                    "model": mi["label"], "credits": round(balance, 2), "tokens_used": output_tokens,
                })
            elif prov in ("openai", "openrouter"):
                client = openai_client if prov == "openai" else openrouter_client
                reply, output_tokens = call_openai_compat(client, mid, msgs)
                cost = round(output_tokens * cpt, 6)
                balance, _ = store.deduct_credits(user["id"], cost)
                store.save_credit_entry(user["id"], -cost, f"AI chat ({mi['label']})")
                session["latest_reply"] = reply
                session["status"] = "done"
                return jsonify({
                    "session_id": session_id, "reply": reply,
                    "tool_calls": [], "plan": None, "status": "done",
                    "model": mi["label"], "credits": round(balance, 2), "tokens_used": output_tokens,
                })
            else:
                reply, output_tokens = call_gemini(mid, msgs)
                cost = round(output_tokens * cpt, 6)
                balance, _ = store.deduct_credits(user["id"], cost)
                store.save_credit_entry(user["id"], -cost, f"AI chat ({mi['label']})")
                session["latest_reply"] = reply
                session["status"] = "done"
                return jsonify({
                    "session_id": session_id, "reply": reply,
                    "tool_calls": [], "plan": None, "status": "done",
                    "model": mi["label"], "credits": round(balance, 2), "tokens_used": output_tokens,
                })

        elif mode == "agent":
            if prov != "anthropic":
                return jsonify({
                    "session_id": session_id,
                    "reply": "Agent mode requires a Claude model.",
                    "tool_calls": [], "plan": None, "status": "done",
                    "model": mi["label"], "credits": round(balance, 2), "tokens_used": 0,
                })
            session["approved"] = False
            session["step_count"] = 0
            session["pending_tool_call"] = None
            session["status"] = "planning"
            pm = build_chat_messages(session, user_message, context, conv_id=conv_id)
            pm.append({"role": "user", "content": "Produce a numbered execution plan only. Do not call any tools yet."})
            r = call_anthropic(mid, pm, max_tokens=2000)
            output_tokens = r.usage.output_tokens
            cost = round(output_tokens * cpt, 6)
            balance, _ = store.deduct_credits(user["id"], cost)
            store.save_credit_entry(user["id"], -cost, f"Agent plan ({mi['label']})")
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
        return jsonify({"session_id": session_id, "reply": "Agent mode requires a Claude model.", "tool_calls": [], "plan": session.get("plan"), "status": "error", "credits": 0, "tokens_used": 0}), 400

    balance, _ = store.get_credits(user["id"])
    if balance <= 0:
        return jsonify({"session_id": session_id, "reply": "No credits remaining.", "tool_calls": [], "plan": session.get("plan"), "status": "error", "credits": round(balance, 2), "tokens_used": 0}), 403

    try:
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
            "id": auto_ckpt_id, "label": f"Before: {user_msg_label}",
            "created_at": int(time.time() * 1000), "scripts": {},
            "auto": True, "session_id": session_id,
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
        store.save_credit_entry(user["id"], -cost, f"Agent execute ({mi['label']})")
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
                "status": "tool_requested", "credits": round(balance, 2), "tokens_used": output_tokens,
            })
        session["latest_reply"] = ft
        session["status"] = "done"
        return jsonify({
            "session_id": session_id, "reply": ft,
            "tool_calls": [], "plan": session["plan"],
            "status": "done", "credits": round(balance, 2), "tokens_used": output_tokens,
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

    pending_tc = session.get("pending_tool_call")
    resolved = 0
    while pending_tc and pending_tc.get("name") in SERVER_SIDE_TOOLS and resolved < 5:
        synthetic = _resolve_server_tool(session, pending_tc)
        pending_tc = _continue_agent_with_result(session, pending_tc, synthetic)
        resolved += 1

    if not session.get("pending_tool_call") and session.get("restore_queue") and session.get("status") == "restoring":
        next_write = session["restore_queue"].pop(0)
        session["pending_tool_call"] = next_write

    if not session.get("pending_tool_call"):
        now_t = time.time()
        with workspace_calls_lock:
            expired_ws = [k for k, v in workspace_calls.items() if now_t - v["created_at"] > 120]
            for k in expired_ws:
                del workspace_calls[k]
            for req_id, wc in workspace_calls.items():
                if wc["status"] == "pending" and wc["plugin_id"] == pid:
                    wc["status"] = "sent"
                    session["pending_tool_call"] = wc["tool_call"]
                    break

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

    pc = session.get("pending_tool_call")
    if pc and isinstance(pc.get("id"), str) and pc["id"].startswith("ws-"):
        req_id = pc["id"][3:]
        tool_result_data = data.get("tool_result", {})
        with workspace_calls_lock:
            if req_id in workspace_calls:
                workspace_calls[req_id]["status"] = "done"
                workspace_calls[req_id]["result"] = tool_result_data
        session["pending_tool_call"] = None
        return jsonify({"reply": "ok", "status": "ok"})

    mk = session.get("model_key", DEFAULT_MODEL)
    mi = resolve_model(mk)
    cpt = mi["credit_per_token"]

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
            store.save_credit_entry(user_id, -cost, f"Agent tool ({mi['label']})")
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
        return jsonify({"reply": f"Error processing tool result: {error_str[:300]}", "status": "error"}), 200

# ═══ CONVERSATION ROUTES

@app.route("/api/conversations", methods=["GET"])
@require_auth
def get_conversations():
    user = request.user
    conv_list = store.get_conv_list(user["id"])
    return jsonify(conv_list)

@app.route("/api/conversations", methods=["POST"])
@require_auth
def save_conversation():
    user = request.user
    data = request.get_json(force=True)
    conv_id = data.get("id") or f"conv-{uuid.uuid4()}"
    store.save_conv(user["id"], conv_id, data)
    return jsonify({"ok": True, "id": conv_id})

@app.route("/api/conversations/<conv_id>", methods=["GET"])
@require_auth
def get_conversation(conv_id):
    user = request.user
    conv = store.get_conv(conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    if conv.get("user_id") and conv["user_id"] != user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(conv)

@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
@require_auth
def delete_conversation(conv_id):
    user = request.user
    store.delete_conv(user["id"], conv_id)
    return jsonify({"ok": True})

@app.route("/api/conversations/<conv_id>/rename", methods=["POST"])
@require_auth
def rename_conversation(conv_id):
    user = request.user
    data = request.get_json(force=True) or {}
    new_title = (data.get("title") or "").strip()
    if not new_title:
        return jsonify({"error": "Title required"}), 400
    if len(new_title) > 100:
        new_title = new_title[:100]
    conv = store.get_conv(conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    if conv.get("user_id") and conv["user_id"] != user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    conv["title"] = new_title
    store.save_conv(user["id"], conv_id, conv)
    return jsonify({"ok": True, "title": new_title})

# ═══ MEMORY ROUTES (per-conversation project facts)

@app.route("/api/memory", methods=["GET"])
@require_auth
def memory_list():
    user = request.user
    conv_id = request.args.get("conv_id", "").strip()
    if not conv_id:
        return jsonify({"memories": []})
    conv = store.get_conv(conv_id)
    if conv and conv.get("user_id") and conv["user_id"] != user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({"memories": store.list_memories(conv_id)})


@app.route("/api/memory", methods=["POST"])
@require_auth
def memory_add():
    user = request.user
    data = request.get_json(force=True) or {}
    conv_id = (data.get("conv_id") or "").strip()
    text = (data.get("text") or data.get("fact") or "").strip()
    if not conv_id or not text:
        return jsonify({"error": "conv_id and text required"}), 400
    conv = store.get_conv(conv_id)
    if conv and conv.get("user_id") and conv["user_id"] != user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    item = store.add_memory(conv_id, text, source="user")
    if not item:
        return jsonify({"error": "Could not save"}), 400
    return jsonify({"ok": True, "memory": item})


@app.route("/api/memory/<mem_id>", methods=["DELETE"])
@require_auth
def memory_delete(mem_id):
    user = request.user
    conv_id = request.args.get("conv_id", "").strip()
    if not conv_id:
        return jsonify({"error": "conv_id required"}), 400
    conv = store.get_conv(conv_id)
    if conv and conv.get("user_id") and conv["user_id"] != user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    ok = store.delete_memory(conv_id, mem_id)
    return jsonify({"ok": ok})


# ═══ ACCOUNT (plan + spending cap + spend tracking)

@app.route("/api/account", methods=["GET"])
@require_auth
def account_info():
    user = request.user
    return jsonify({
        "plan": store.get_user_plan(user["id"]),
        "spending_cap": store.get_spending_cap(user["id"]),
        "spent_today": round(store.get_daily_spend(user["id"]), 4),
    })


@app.route("/api/spending_cap", methods=["POST"])
@require_auth
def account_set_cap():
    user = request.user
    data = request.get_json(force=True) or {}
    try:
        cap = float(data.get("cap", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "cap must be a number"}), 400
    if cap < 0:
        cap = 0
    new_cap = store.set_spending_cap(user["id"], cap)
    return jsonify({"ok": True, "cap": new_cap})


@app.route("/api/plan", methods=["POST"])
@require_auth
def account_set_plan():
    """Owner-only: set a user's plan tier."""
    user = request.user
    if user["id"] != OWNER_ID:
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(force=True) or {}
    target_uid = (data.get("user_id") or user["id"]).strip()
    plan = (data.get("plan") or "").strip().lower()
    if plan not in store.VALID_PLANS:
        return jsonify({"error": f"plan must be one of {list(store.VALID_PLANS)}"}), 400
    store.set_user_plan(target_uid, plan)
    return jsonify({"ok": True, "user_id": target_uid, "plan": plan})


# ═══ CHECKPOINT ROUTES

@app.route("/api/checkpoints", methods=["GET"])
@require_auth
def get_checkpoints():
    user = request.user
    ckpts = store.get_checkpoints(user["id"])
    result = []
    for ckpt_id, ckpt in ckpts.items():
        result.append({
            "id": ckpt_id,
            "label": ckpt.get("label", ""),
            "created_at": ckpt.get("created_at", 0),
            "scripts_count": len(ckpt.get("scripts", {})),
            "script_names": list(ckpt.get("scripts", {}).keys()),
            "auto": ckpt.get("auto", False),
        })
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return jsonify(result)

@app.route("/api/checkpoints/<checkpoint_id>", methods=["DELETE"])
@require_auth
def delete_checkpoint(checkpoint_id):
    user = request.user
    if store.delete_checkpoint(user["id"], checkpoint_id):
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404

@app.route("/api/checkpoints/<checkpoint_id>/restore", methods=["POST"])
@require_auth
def restore_checkpoint_route(checkpoint_id):
    user = request.user
    data = request.get_json(force=True) or {}
    ckpt = store.get_checkpoint(user["id"], checkpoint_id)
    if not ckpt:
        return jsonify({"error": "Checkpoint not found"}), 404

    scripts = ckpt.get("scripts", {})
    if not scripts:
        return jsonify({"error": "No scripts to restore"}), 400

    sid = data.get("session_id")
    if not sid:
        active = store.get_user_plugin(user["id"])
        sid = active.get("session_id") if active else None
    if not sid:
        return jsonify({"error": "No active session. Connect Studio first."}), 400

    session = get_session(sid)

    restore_queue = []
    for script_name, source in scripts.items():
        restore_queue.append({
            "id": f"restore-{uuid.uuid4()}",
            "name": "write_script",
            "arguments": {"name": script_name, "code": source},
        })

    session["restore_queue"] = restore_queue
    session["restore_scripts_count"] = len(scripts)
    session["status"] = "restoring"
    session["pending_tool_call"] = restore_queue[0] if restore_queue else None

    return jsonify({
        "ok": True,
        "scripts_count": len(scripts),
        "checkpoint_id": checkpoint_id,
        "label": ckpt.get("label", ""),
    })

# ═══ WORKSPACE ROUTES

@app.route("/workspace/call", methods=["POST"])
@require_auth
def workspace_call():
    data = request.get_json(force=True)
    tool_name = data.get("tool")
    tool_args = data.get("args", {})
    user = request.user

    active_session = store.get_user_plugin(user["id"])
    if not active_session:
        return jsonify({"error": "Studio not connected"}), 400

    pid = active_session.get("plugin_id")
    with plugin_registry_lock:
        info = plugin_registry.get(pid)
        if not info or time.time() - info.get("last_seen", 0) > PLUGIN_OFFLINE_AFTER:
            return jsonify({"error": "Studio not connected"}), 400
        sid = active_session.get("session_id") or str(uuid.uuid4())

    req_id = str(uuid.uuid4())
    tc = {
        "id": f"ws-{req_id}",
        "name": tool_name,
        "arguments": tool_args,
    }
    with workspace_calls_lock:
        workspace_calls[req_id] = {
            "tool_call": tc,
            "status": "pending",
            "result": None,
            "user_id": user["id"],
            "plugin_id": pid,
            "session_id": sid,
            "created_at": time.time(),
        }
    return jsonify({"ok": True, "req_id": req_id})


@app.route("/workspace/result/<req_id>", methods=["GET"])
@require_auth
def workspace_result(req_id):
    with workspace_calls_lock:
        wc = workspace_calls.get(req_id)
    if not wc:
        return jsonify({"error": "Not found"}), 404
    if wc["user_id"] != request.user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({"ok": True, "status": wc["status"], "result": wc.get("result")})


@app.route("/workspace/complete", methods=["POST"])
@require_auth
def workspace_complete():
    data = request.get_json(force=True)
    code_before = (data.get("code_before") or "")[-2000:]
    code_after = (data.get("code_after") or "")[:500]
    script_name = data.get("script_name") or "Script"

    user = request.user
    balance, _ = store.get_credits(user["id"])
    if balance <= 0:
        return jsonify({"error": "No credits"}), 403

    mi = resolve_model("haiku")
    prompt = (
        f"You are a Roblox Luau code completion engine. Script: {script_name}\n\n"
        f"Code before cursor:\n```lua\n{code_before}\n```\n\n"
        f"Code after cursor:\n```lua\n{code_after}\n```\n\n"
        "Return ONLY the completion text to insert at the cursor. "
        "No explanation, no markdown fences. Raw Luau code only."
    )
    try:
        r = call_anthropic(mi["id"], [{"role": "user", "content": prompt}], max_tokens=300)
        completion = "".join(
            b.text for b in r.content if hasattr(b, "type") and b.type == "text"
        ).strip()
        output_tokens = r.usage.output_tokens
        cost = round(output_tokens * mi["credit_per_token"], 6)
        store.deduct_credits(user["id"], cost)
        store.save_credit_entry(user["id"], -cost, f"Workspace completion ({mi['label']})")
        return jsonify({"ok": True, "completion": completion})
    except Exception as e:
        logging.exception("workspace complete failed")
        return jsonify({"error": str(e)}), 500


# ── Workspace offline cache routes (project-aware) ──

def _ws_pid(data_or_args):
    """Extract project_id from request data or args, defaulting to 'default'."""
    pid = data_or_args.get("project_id") or data_or_args.get("pid") or "default"
    return str(pid)[:128]


@app.route("/workspace/scripts/cached", methods=["GET"])
@require_auth
def ws_cached_scripts():
    user = request.user
    pid = _ws_pid(request.args)
    cached = store.ws_get_all(user["id"], pid)
    return jsonify({"ok": True, "scripts": list(cached.values()), "project_id": pid})


@app.route("/workspace/script/content", methods=["GET"])
@require_auth
def ws_script_content():
    user = request.user
    name = request.args.get("name", "")
    pid = _ws_pid(request.args)
    if not name:
        return jsonify({"error": "name required"}), 400
    entry = store.ws_get_script(user["id"], name, pid)
    if not entry:
        return jsonify({"error": "Not in cache"}), 404
    return jsonify({"ok": True, "entry": entry})


@app.route("/workspace/script/save", methods=["POST"])
@require_auth
def ws_script_save_local():
    data = request.get_json(force=True)
    name = data.get("name", "")
    content = data.get("content", "")
    pid = _ws_pid(data)
    if not name:
        return jsonify({"error": "name required"}), 400
    user = request.user
    entry = store.ws_save_local(user["id"], name, content, pid)
    return jsonify({"ok": True, "dirty": entry["dirty"]})


@app.route("/workspace/script/history", methods=["GET"])
@require_auth
def ws_script_history():
    user = request.user
    name = request.args.get("name", "")
    pid = _ws_pid(request.args)
    if not name:
        return jsonify({"error": "name required"}), 400
    history = store.ws_get_history(user["id"], name, pid)
    return jsonify({"ok": True, "history": history, "name": name})


@app.route("/workspace/script/revert", methods=["POST"])
@require_auth
def ws_script_revert():
    data = request.get_json(force=True)
    name = data.get("name", "")
    version_idx = data.get("version_idx", 0)
    pid = _ws_pid(data)
    if not name:
        return jsonify({"error": "name required"}), 400
    user = request.user
    content = store.ws_revert_to(user["id"], name, int(version_idx), pid)
    if content is None:
        return jsonify({"error": "Version not found"}), 404
    return jsonify({"ok": True, "content": content})


@app.route("/workspace/sync", methods=["POST"])
@require_auth
def ws_sync():
    data = request.get_json(force=True)
    name = data.get("name", "")
    studio_content = data.get("studio_content", "")
    pid = _ws_pid(data)
    user = request.user
    uid = user["id"]

    if not name:
        return jsonify({"error": "name required"}), 400

    entry = store.ws_get_script(uid, name, pid)
    if not entry:
        store.ws_pull_script(uid, name, studio_content, pid)
        return jsonify({"ok": True, "action": "up_to_date", "content": studio_content})

    base = entry.get("base", "")
    local = entry.get("local", "")
    studio_changed = studio_content != base
    local_changed = local != base

    if not studio_changed and not local_changed:
        return jsonify({"ok": True, "action": "up_to_date", "content": local})

    if studio_changed and not local_changed:
        store.ws_pull_script(uid, name, studio_content, pid)
        return jsonify({"ok": True, "action": "studio_ahead", "content": studio_content})

    if local_changed and not studio_changed:
        return jsonify({"ok": True, "action": "local_ahead", "content": local})

    # Both changed — AI merge
    balance, _ = store.get_credits(uid)
    if balance <= 0:
        return jsonify({"ok": True, "action": "conflict_no_credits", "content": local,
                        "studio_content": studio_content})

    mi = resolve_model("haiku")
    prompt = (
        f"You are a Roblox Luau code merge assistant.\n\n"
        f"Script: {name}\n\n"
        f"=== BASE ===\n```lua\n{base[:3000]}\n```\n\n"
        f"=== LOCAL (Rux workspace) ===\n```lua\n{local[:3000]}\n```\n\n"
        f"=== STUDIO ===\n```lua\n{studio_content[:3000]}\n```\n\n"
        "Produce a clean merged Luau script. Output ONLY the merged Luau code, no explanation, no fences."
    )
    try:
        r = call_anthropic(mi["id"], [{"role": "user", "content": prompt}], max_tokens=4096)
        merged = "".join(b.text for b in r.content if hasattr(b, "type") and b.type == "text").strip()
        cost = round(r.usage.output_tokens * mi["credit_per_token"], 6)
        store.deduct_credits(uid, cost)
        store.save_credit_entry(uid, -cost, f"Workspace merge ({mi['label']})")
        store.ws_pull_script(uid, name, studio_content, pid)
        store.ws_save_local(uid, name, merged, pid)
        return jsonify({"ok": True, "action": "merged", "content": merged, "studio_content": studio_content})
    except Exception as e:
        logging.exception("ws merge failed")
        return jsonify({"ok": True, "action": "conflict", "content": local,
                        "studio_content": studio_content, "error": str(e)})


@app.route("/workspace/push", methods=["POST"])
@require_auth
def ws_push_script():
    data = request.get_json(force=True)
    name = data.get("name", "")
    ws_project_id = _ws_pid(data)
    user = request.user
    uid = user["id"]

    entry = store.ws_get_script(uid, name, ws_project_id)
    if not entry:
        return jsonify({"error": "Script not in cache"}), 404

    active_session = store.get_user_plugin(uid)
    if not active_session:
        return jsonify({"error": "Studio not connected"}), 400

    plugin_pid = active_session.get("plugin_id")
    with plugin_registry_lock:
        info = plugin_registry.get(plugin_pid)
        if not info or time.time() - info.get("last_seen", 0) > PLUGIN_OFFLINE_AFTER:
            return jsonify({"error": "Studio not connected"}), 400

    code = entry["local"]
    req_id = str(uuid.uuid4())
    tc = {"id": f"ws-{req_id}", "name": "write_script", "arguments": {"name": name, "code": code}}
    with workspace_calls_lock:
        workspace_calls[req_id] = {
            "tool_call": tc, "status": "pending", "result": None,
            "user_id": uid, "plugin_id": plugin_pid,
            "session_id": active_session.get("session_id") or str(uuid.uuid4()),
            "created_at": time.time(),
        }
    return jsonify({"ok": True, "req_id": req_id, "name": name})


@app.route("/workspace/push/confirm", methods=["POST"])
@require_auth
def ws_push_confirm():
    data = request.get_json(force=True)
    name = data.get("name", "")
    content = data.get("content", "")
    ws_project_id = _ws_pid(data)
    user = request.user
    store.ws_mark_pushed(user["id"], name, content, ws_project_id)
    return jsonify({"ok": True})


# ═══ STATUS ROUTE

@app.route("/status", methods=["GET"])
@require_auth
def status_check():
    user = request.user
    active_session = store.get_user_plugin(user["id"])
    plugin_connected = False
    sid = None
    if active_session:
        pid = active_session.get("plugin_id")
        with plugin_registry_lock:
            if pid and pid in plugin_registry and time.time() - plugin_registry[pid]["last_seen"] < PLUGIN_OFFLINE_AFTER:
                plugin_connected = True
                sid = active_session.get("session_id")
    balance, _ = store.get_credits(user["id"])
    return jsonify({
        "plugin_connected": plugin_connected,
        "session_id": sid,
        "credits": round(balance, 2),
    })

# ═══ PAGE ROUTES

@app.route("/app")
def app_page():
    return render_template("index.html")

@app.route("/")
def index():
    return render_template("landing.html")

@app.route("/code/<token>")
def code_page(token):
    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)