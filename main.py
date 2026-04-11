import os
import json
import uuid
import time
import re
import secrets
from flask import Flask, request, jsonify, render_template
from threading import Lock

try:
    import bcrypt
    HAS_BCRYPT = True
except ImportError:
    HAS_BCRYPT = False

try:
    import replit
    HAS_REPLIT = True
except ImportError:
    HAS_REPLIT = False

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from anthropic import Anthropic

app = Flask(__name__)

anthropic_client = Anthropic(
    api_key=os.environ.get("AI_INTEGRATIONS_ANTHROPIC_API_KEY"),
    base_url=os.environ.get("AI_INTEGRATIONS_ANTHROPIC_BASE_URL"),
)

if GEMINI_AVAILABLE:
    gemini_client = google_genai.Client(
        api_key=os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY"),
        http_options={
            "api_version": "",
            "base_url": os.environ.get("AI_INTEGRATIONS_GEMINI_BASE_URL"),
        },
    )
else:
    gemini_client = None

MAX_AGENT_STEPS = 20
TOKEN_COST = 0.005
STARTING_CREDITS = 10.0
CODE_EXPIRY_MS = 5 * 60 * 1000
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

MODELS = {
    "gemini-flash": {"id": "gemini-2.5-flash", "provider": "google", "label": "Gemini Flash", "badge": "Fast"},
    "gemini-pro": {"id": "gemini-2.5-pro", "provider": "google", "label": "Gemini Pro", "badge": "Smart"},
    "sonnet": {"id": "claude-sonnet-4-6", "provider": "anthropic", "label": "Claude Sonnet", "badge": "Balanced"},
    "opus": {"id": "claude-opus-4-6", "provider": "anthropic", "label": "Claude Opus", "badge": "Powerful"},
}
DEFAULT_MODEL = "sonnet"

# ═══ KV HELPER ═══
class KV:
    def __init__(self):
        self._mem = {}
    def get(self, key):
        if HAS_REPLIT:
            try:
                val = replit.db[key]
                return json.loads(val)
            except Exception:
                return None
        return self._mem.get(key)
    def set(self, key, value):
        if HAS_REPLIT:
            replit.db[key] = json.dumps(value)
        self._mem[key] = value
    def delete(self, key):
        if HAS_REPLIT:
            try: del replit.db[key]
            except Exception: pass
        self._mem.pop(key, None)

kv = KV()

# ═══ IN-MEMORY STATE ═══
sessions = {}
sessions_lock = Lock()
plugin_registry = {}
plugin_registry_lock = Lock()
pending_connections = {}
pending_connections_lock = Lock()

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

def get_user_from_token(token):
    if not token:
        return None
    s = kv.get(f"session:{token}")
    if not s:
        return None
    return kv.get(f"user:id:{s['user_id']}")

def require_auth(f):
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user = get_user_from_token(token)
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        request.user = user
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

# ═══ CREDIT HELPERS ═══
def get_credits(user_id):
    val = kv.get(f"credits:{user_id}")
    return val if val is not None else STARTING_CREDITS

def track_tokens(user_id, output_tokens):
    if output_tokens <= 0:
        return get_credits(user_id)
    balance = get_credits(user_id)
    cost = output_tokens * TOKEN_COST
    balance = max(0, balance - cost)
    kv.set(f"credits:{user_id}", balance)
    return balance

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
                "accumulated_reply": "",
            }
        return sessions[session_id]

def content_blocks_to_dicts(blocks):
    result = []
    for b in blocks:
        if hasattr(b, 'type'):
            if b.type == "text":
                result.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                result.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, dict):
            result.append(b)
    return result

def build_context(data):
    return {
        "current_script_name": data.get("current_script_name"),
        "current_script_source": data.get("current_script_source"),
        "selected_instance": data.get("selected_instance"),
    }

def build_chat_messages(session, user_message, context):
    content = f"User message:\n{user_message}\n\nCurrent script name:\n{context.get('current_script_name')}\n\nSelected instance:\n{json.dumps(context.get('selected_instance'), indent=2)}"
    messages = list(session["conversation"])
    messages.append({"role": "user", "content": content})
    return messages

def resolve_model(key):
    return MODELS.get(key, MODELS[DEFAULT_MODEL])

# ═══ AI HELPERS ═══
SYSTEM_PROMPT = """You are Rux, a Roblox Studio and Luau expert AI assistant connected to a live Roblox Studio plugin via a tool bridge.

TOOLS:
- You have direct access to tools (read_script, write_script, list_scripts, get_script_tree, search_code, create_script, delete_script, snapshot_script, restore_script, diff_script, check_errors, get_instance_tree, get_properties, set_property, find_instance, get_selection, get_current_script, get_place_metadata, find_usages, get_output_log, get_error_log).
- Use one tool at a time and wait for the result before proceeding.
- Always use list_scripts or get_script_tree first before trying to read a specific script by name, so you know the exact name.
- get_output_log and get_error_log return whatever the plugin captured — they may be empty because Roblox does not expose the full Output window to plugins. Tell the user to check the Studio Output window directly if needed.

RULES:
- Be precise, safe, and incremental.
- In agent mode, first produce a numbered plan before using tools.
- In chat mode, call tools directly to answer the user's question — no plan needed.
- Prefer inspection before writing.
- Before editing any script, call snapshot_script first.
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
    {"name": "list_scripts", "description": "List all script names in the current place.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_script_tree", "description": "Get a JSON tree or list of scripts and their paths.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "check_errors", "description": "Attempt to detect syntax or script issues for a named script.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_output_log", "description": "Get recent output log lines available through the plugin.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_error_log", "description": "Get recent error log lines available through the plugin.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "search_code", "description": "Search all scripts for a query string.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find_usages", "description": "Search all scripts for usages of a variable or function name.", "input_schema": {"type": "object", "properties": {"variable_name": {"type": "string"}}, "required": ["variable_name"]}},
    {"name": "get_instance_tree", "description": "Get the Explorer instance tree.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_properties", "description": "Get properties for an instance path.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string"}}, "required": ["instance_path"]}},
    {"name": "set_property", "description": "Set a property on an instance path.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string"}, "property": {"type": "string"}, "value": {}}, "required": ["instance_path", "property", "value"]}},
    {"name": "find_instance", "description": "Find an instance anywhere in the game by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_selection", "description": "Return the current Explorer selection.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_current_script", "description": "Return the currently selected or active script name and source if available.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_place_metadata", "description": "Return game name, place id, and version.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "snapshot_script", "description": "Save a snapshot of a script before modification.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "diff_script", "description": "Show differences against the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "restore_script", "description": "Restore a script to the last snapshot.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

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
    existing = kv.get(f"user:email:{email}")
    if existing:
        return jsonify({"error": "Email already registered"}), 400
    user_id = str(uuid.uuid4())
    kv.set(f"user:id:{user_id}", {
        "id": user_id, "email": email,
        "password_hash": hash_password(password),
        "roblox_id": roblox_id, "created_at": int(time.time()),
    })
    kv.set(f"user:email:{email}", {"id": user_id})
    if roblox_id:
        kv.set(f"user:roblox:{roblox_id}", {"id": user_id, "email": email})
    kv.set(f"credits:{user_id}", STARTING_CREDITS)
    token = str(uuid.uuid4())
    kv.set(f"session:{token}", {"user_id": user_id, "created_at": int(time.time())})
    return jsonify({"token": token, "user": {"id": user_id, "email": email}})

@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user = kv.get(f"user:email:{email}")
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401
    full = kv.get(f"user:id:{user['id']}")
    if not full or not verify_password(password, full["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401
    token = str(uuid.uuid4())
    kv.set(f"session:{token}", {"user_id": full["id"], "created_at": int(time.time())})
    return jsonify({"token": token, "user": {"id": full["id"], "email": full["email"]}})

@app.route("/auth/me", methods=["GET"])
@require_auth
def auth_me():
    user = request.user
    credits = get_credits(user["id"])
    session_id = kv.get(f"user_plugin:{user['id']}")
    sid = session_id.get("session_id") if session_id else None
    return jsonify({
        "user": {"id": user["id"], "email": user["email"]},
        "credits": credits,
        "session_id": sid,
    })

@app.route("/auth/logout", methods=["POST"])
@require_auth
def auth_logout():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    kv.delete(f"session:{token}")
    return jsonify({"ok": True})

# ═══ CONVERSATION ROUTES ═══
@app.route("/conversations", methods=["GET"])
@require_auth
def get_conversations():
    user_id = request.user["id"]
    convs = kv.get(f"user_convs:{user_id}") or []
    return jsonify(convs)

@app.route("/conversations/<conv_id>", methods=["DELETE"])
@require_auth
def delete_conversation(conv_id):
    user_id = request.user["id"]
    key = f"user_convs:{user_id}"
    convs = kv.get(key) or []
    convs = [c for c in convs if c.get("id") != conv_id]
    kv.set(key, convs)
    kv.delete(f"user_conv:{user_id}:{conv_id}")
    return jsonify({"ok": True})

@app.route("/conversations", methods=["POST"])
@require_auth
def save_conversation():
    user_id = request.user["id"]
    data = request.get_json(force=True)
    conv_id = data.get("id")
    key = f"user_convs:{user_id}"
    convs = kv.get(key) or []
    idx = next((i for i, c in enumerate(convs) if c.get("id") == conv_id), None)
    if idx is not None:
        convs[idx] = data
    else:
        convs.append(data)
    kv.set(key, convs)
    return jsonify({"ok": True})

# ═══ CONNECTION ROUTES ═══
@app.route("/connect/code", methods=["GET"])
@require_auth
def connect_code():
    clean_expired_codes()
    code = generate_code()
    session_id = str(uuid.uuid4())
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
    if creator_id:
        u = kv.get(f"user:roblox:{str(creator_id)}")
        if u:
            session_id = str(uuid.uuid4())
            method = "auto"
            kv.set(f"user_plugin:{u['id']}", {"plugin_id": plugin_id, "session_id": session_id})
    if not session_id and code:
        clean_expired_codes()
        with pending_connections_lock:
            pending = pending_connections.get(code)
            if pending:
                now = int(time.time() * 1000)
                if now - pending["created_at"] <= CODE_EXPIRY_MS:
                    session_id = pending["session_id"]
                    method = "code"
                    kv.set(f"user_plugin:{pending['user_id']}", {"plugin_id": plugin_id, "session_id": session_id})
                    del pending_connections[code]
    if not session_id:
        return jsonify({"ok": False, "error": "Invalid or expired code"}), 400
    with plugin_registry_lock:
        plugin_registry[plugin_id] = {
            "session_id": session_id, "plugin_id": plugin_id,
            "last_seen": time.time(), "status": "connected",
        }
    return jsonify({"ok": True, "session_id": session_id, "method": method})

@app.route("/plugin/heartbeat", methods=["POST"])
def plugin_heartbeat():
    data = request.get_json(force=True)
    pid = data.get("plugin_id")
    sid = data.get("session_id")
    if pid:
        with plugin_registry_lock:
            if pid not in plugin_registry:
                plugin_registry[pid] = {"session_id": sid, "plugin_id": pid, "last_seen": 0, "status": "connected"}
            plugin_registry[pid]["last_seen"] = time.time()
            plugin_registry[pid]["status"] = data.get("status", "connected")
    return jsonify({"ok": True})

@app.route("/plugin/poll", methods=["POST"])
def plugin_poll():
    data = request.get_json(force=True)
    sid = data.get("session_id")
    session = get_session(sid)
    return jsonify({"status_message": session["status"], "tool_call": session.get("pending_tool_call")})

@app.route("/plugin/tool_result", methods=["POST"])
def plugin_tool_result():
    data = request.get_json(force=True)
    sid = data.get("session_id")
    session = get_session(sid)
    mk = session.get("model_key", DEFAULT_MODEL)
    mi = resolve_model(mk)
    if mi["provider"] == "google":
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({"reply": "Tools not supported for Google models.", "status": "error"}), 400
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
        tr = {"type": "tool_result", "tool_use_id": pc["id"], "content": json.dumps(data.get("tool_result"), ensure_ascii=False)}
        cont = prior + [{"role": "user", "content": [tr]}]
        r = call_anthropic(mi["id"], cont, tools=TOOL_DEFINITIONS)
        session["agent_messages"] = cont + [{"role": "assistant", "content": content_blocks_to_dicts(r.content)}]
        output_tokens = getattr(r, 'usage', {}).get('output_tokens', 0) or 0
        if output_tokens > 0:
            track_tokens(request.user["id"], output_tokens)
        nt = None
        ft = ""
        for b in r.content:
            if b.type == "tool_use":
                nt = {"id": b.id, "name": b.name, "arguments": b.input}
            elif b.type == "text":
                ft += b.text
        if nt:
            session["pending_tool_call"] = nt
            session["status"] = "running"
            return jsonify({"reply": ft or "Tool processed.", "status": "tool_requested", "tool_call": nt})
        session["status"] = "done"
        final_reply = session.get("accumulated_reply", "") or ft
        session["latest_reply"] = final_reply
        session["accumulated_reply"] = ""
        return jsonify({"reply": final_reply, "status": "done"})
    except anthropic.APIError as e:
        session["status"] = "error"
        session["pending_tool_call"] = None
        output_tokens = 0
        try:
            parsed = json.loads(str(e.body))
            if parsed.get("error", {}).get("type") == "invalid_api_key":
                msg = "API key error."
            elif "context_length" in str(e):
                msg = "Conversation too long. Start a new conversation."
            else:
                msg = "Session lost. Start a new conversation."
        except Exception:
            msg = "Session lost. Start a new conversation."
        if output_tokens > 0:
            track_tokens(request.user["id"], output_tokens)
        return jsonify({"reply": msg, "status": "error"}), 200
    except Exception as e:
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({"reply": "Internal error. Start a new conversation.", "status": "error"}), 500

# ═══ AI ROUTES ═══
@app.route("/ai", methods=["POST"])
@require_auth
def ai():
    user = request.user
    balance = get_credits(user["id"])
    if balance < 0.001:
        _, _, next_at = 0, _ == get_credits_and_next(user["id"])
        return jsonify({"error": "no_credits", "balance": 0, "next_credit_at": next_at}), 403
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

    mi = resolve_model(model_key)
    mid, prov = mi["id"], mi["provider"]
    output_tokens = 0

    try:
        if prov == "anthropic":
            r = call_anthropic(mid, build_chat_messages(session, user_message, context), tools=TOOL_DEFINITIONS)
            output_tokens = getattr(r, 'usage', {}).get('output_tokens', 0) or 0
            tc = None
            reply_text = ""
            for b in r.content:
                if b.type == "tool_use":
                    tc = {"id": b.id, "name": b.name, "arguments": b.input}
                elif b.type == "text":
                    reply_text += b.text
            if tc:
                session["pending_tool_call"] = tc
                session["agent_messages"] = [{"role": "assistant", "content": content_blocks_to_dicts(r.content)}]
                session["status"] = "running"
                if output_tokens > 0:
                    track_tokens(user["id"], output_tokens)
                return jsonify({"session_id": session_id, "reply": reply_text or "", "tool_calls": [tc], "plan": None, "status": "tool_requested", "model": mi["label"], "credits": balance})
            session["latest_reply"] = reply_text
            session["status"] = "done"
            conversation_history.append({"role": "assistant", "content": reply_text})
            if output_tokens > 0:
                track_tokens(user["id"], output_tokens)
            return jsonify({"session_id": session_id, "reply": reply_text, "tool_calls": [], "plan": None, "status": "done", "model": mi["label"], "credits": balance})
        else:
            text, usage = call_gemini(mid, build_chat_messages(session, user_message, context))
            output_tokens = (usage or {}).get("output_tokens", 0) or 0
            if output_tokens > 0:
                track_tokens(user["id"], output_tokens)
            session["latest_reply"] = text
            session["status"] = "done"
            conversation_history.append({"role": "assistant", "content": text})
            return jsonify({"session_id": session_id, "reply": text, "tool_calls": [], "plan": None, "status": "done", "model": mi["label"], "credits": balance})

    except anthropic.APIError as e:
        output_tokens = 0
        try:
            parsed = json.loads(str(e.body))
            if parsed.get("error", {}).get("type") == "invalid_api_key":
                msg = "API key error."
            elif "context_length" in str(e):
                msg = "Conversation too long. Start a new conversation."
            else:
                msg = "Session lost. Start a new conversation."
        except Exception:
            msg = "Session lost. Start a new conversation."
        if output_tokens > 0:
            track_tokens(user["id"], output_tokens)
        session["status"] = "error"
        return jsonify({"reply": msg, "status": "error", "credits": balance}), 200
    except Exception as e:
        session["status"] = "error"
        return jsonify({"reply": "Internal error.", "status": "error", "credits": balance}), 500

def get_credits_and_next(user_id):
    balance = get_credits(user_id)
    if balance >= 9.99:
        return balance, 0
    return balance, int(time.time() * 1000) + int(60000)

@app.route("/ai/result/<session_id>", methods=["GET"])
@require_auth
def ai_result(session_id):
    session = get_session(session_id)
    return jsonify({
        "session_id": session_id,
        "status": session.get("status", "idle"),
        "reply": session.get("latest_reply", ""),
        "pending_tool_call": session.get("pending_tool_call"),
        "tool_calls": [session.get("pending_tool_call")] if session.get("pending_tool_call") else [],
    })

@app.route("/ai/approve", methods=["POST"])
@require_auth
def approve_agent():
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
    if mi["provider"] == "google":
        session["status"] = "error"
        return jsonify({"reply": "Agent requires Claude.", "tool_calls": [], "plan": session.get("plan"), "status": "error"}), 400
    output_tokens = 0
    try:
        prior = session.get("agent_messages", [])
        if len(prior) == 0:
            session["status"] = "error"
            return jsonify({"reply": "Session expired. Start a new conversation.", "tool_calls": [], "plan": session.get("plan"), "status": "error"}), 200
        prior.append({"role": "user", "content": "The plan is approved. Start executing now. Use one tool at a time."})
        r = call_anthropic(mi["id"], prior, tools=TOOL_DEFINITIONS)
        session["agent_messages"] = prior + [{"role": "assistant", "content": content_blocks_to_dicts(r.content)}]
        output_tokens = getattr(r, 'usage', {}).get('output_tokens', 0) or 0
        if output_tokens > 0:
            track_tokens(request.user["id"], output_tokens)
        nt = None
        ft = ""
        for b in r.content:
            if b.type == "tool_use":
                nt = {"id": b.id, "name": b.name, "arguments": b.input}
            elif b.type == "text":
                ft += b.text
        if nt:
            session["pending_tool_call"] = nt
            return jsonify({"reply": ft or "Executing.", "tool_calls": [nt], "plan": session["plan"], "status": "tool_requested"})
        session["status"] = "done"
        final_reply = session.get("accumulated_reply", "") or ft
        session["latest_reply"] = final_reply
        session["accumulated_reply"] = ""
        return jsonify({"reply": final_reply, "tool_calls": [], "plan": session["plan"], "status": "done"})
    except Exception as e:
        session["status"] = "error"
        session["pending_tool_call"] = None
        output_tokens = 0
        try:
            msg = "Session lost. Start a new conversation."
        except Exception:
            msg = "Session lost. Start a new conversation."
        if output_tokens > 0:
            track_tokens(request.user["id"], output_tokens)
        return jsonify({"reply": msg, "tool_calls": [], "plan": session.get("plan"), "status": "error"}), 200

# ═══ PLUGIN ROUTES ═══
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
    app.run(host="0.0.0", port=5000, debug=True)