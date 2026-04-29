import os
import json
import uuid
import time
import re
import secrets
import traceback
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
    "gemma-31b": {"id": "google/gemma-4-31b-it", "provider": "openrouter", "label": "Gemma 4 31B", "badge": "Free", "credit_per_token": 0},
    "gemma-26b": {"id": "google/gemma-4-26b-a4b-it", "provider": "openrouter", "label": "Gemma 4 26B", "badge": "Free", "credit_per_token": 0},
}
DEFAULT_MODEL = "qwen-coder"
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

# ═══ AUTH HELPERS ═══
def hash_password(password):
    if not HAS_BCRYPT:
        return password
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

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
    {"name": "snapshot_script", "description": "Save a snapshot of a script before modification.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "diff_script", "description": "Show differences against the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "restore_script", "description": "Restore a script to the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "create_checkpoint", "description": "Save a named checkpoint of one or more scripts.", "input_schema": {"type": "object", "properties": {"label": {"type": "string"}, "scripts": {"type": "object", "additionalProperties": {"type": "string"}}}, "required": ["label", "scripts"]}},
    {"name": "list_checkpoints", "description": "List all checkpoints saved for the current session.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "restore_checkpoint", "description": "Retrieve saved script contents from a checkpoint by ID.", "input_schema": {"type": "object", "properties": {"checkpoint_id": {"type": "string"}}, "required": ["checkpoint_id"]}},
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
        active_plugins = sum(1 for p in plugin_registry.values() if now - p["last_seen"] < 15)
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
                "active": (now - info.get("last_seen", 0)) < 15,
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

    prefs = store.get_preferences(user["id"])
    announcements = store.get_active_announcements()

    return jsonify({
        "user": {"id": user["id"], "email": user["email"], "roblox_id": user.get("roblox_id", "")},
        "credits": {"balance": round(balance, 2), "max": store.MAX_CREDITS, "next_credit_at": next_at},
        "session_id": session_id,
        "preferences": prefs,
        "announcements": announcements,
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


@app.route("/api/roblox/analyze-game", methods=["POST"])
@require_auth
def analyze_roblox_game():
    user = request.user
    data = request.get_json(force=True) or {}
    raw = data.get("place_id") or data.get("url") or ""
    place_id = _extract_place_id(raw)
    if not place_id:
        return jsonify({"error": "Couldn't read a place ID or game URL from that input."}), 400

    balance, _ = store.get_credits(user["id"])
    cost = 1.0
    if balance < cost:
        return jsonify({"error": "Not enough credits — analysis costs 1 credit."}), 402

    try:
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
            "play_url": f"https://www.roblox.com/games/{place_id}",
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
            if pid and pid in plugin_registry and time.time() - plugin_registry[pid]["last_seen"] < 15:
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
            pm = build_chat_messages(session, user_message, context)
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
            if pid and pid in plugin_registry and time.time() - plugin_registry[pid]["last_seen"] < 15:
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