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
MAX_CREDITS = 10
CREDIT_INTERVAL_MS = 6 * 60 * 60 * 1000
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
    data = kv.get(f"credits:{user_id}")
    if not data:
        return MAX_CREDITS, 0
    balance = data.get("balance", 0)
    last_updated = data.get("last_updated", 0)
    now = int(time.time() * 1000)
    if last_updated > 0 and balance < MAX_CREDITS:
        elapsed = now - last_updated
        if elapsed >= CREDIT_INTERVAL_MS:
            add = min(int(elapsed // CREDIT_INTERVAL_MS), MAX_CREDITS - balance)
            balance += add
            last_updated += add * CREDIT_INTERVAL_MS
            kv.set(f"credits:{user_id}", {"balance": balance, "last_updated": last_updated})
    return balance, last_updated

def use_credit(user_id):
    balance, last_updated = get_credits(user_id)
    if balance < 1:
        return False, balance, last_updated
    balance -= 1
    kv.set(f"credits:{user_id}", {"balance": balance, "last_updated": last_updated})
    return True, balance, last_updated

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
            }
        return sessions[session_id]

def append_log(session, message):
    session["logs"].append(message)

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
def call_anthropic(model_id, messages, max_tokens=1500, tools=None):
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
    return resp.text

SYSTEM_PROMPT = """You are Rux, a Roblox Studio and Luau expert AI assistant connected to a local Roblox Studio plugin.
Rules:
- Be precise, safe, and incremental.
- In agent mode, first produce a numbered plan before using tools.
- Use one tool at a time.
- Wait for tool results before continuing.
- Prefer inspection before writing.
- Before editing scripts, use snapshot_script if one does not already exist.
- Keep changes minimal and explain what changed.
- Respect the tool result exactly as returned.
- If a tool fails, recover gracefully and choose the next best step.
- Stop when the task is complete and provide a concise final summary.
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
    {"name": "get_instance_tree", "description": "Get the Explorer instance tree.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_properties", "description": "Get properties for an instance path.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string"}}, "required": ["instance_path"]}},
    {"name": "set_property", "description": "Set a property on an instance path.", "input_schema": {"type": "object", "properties": {"instance_path": {"type": "string"}, "property": {"type": "string"}, "value": {}}, "required": ["instance_path", "property", "value"]}},
    {"name": "find_instance", "description": "Find an instance anywhere in the game by name.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_selection", "description": "Return the current Explorer selection.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_current_script", "description": "Return the currently selected or active script name and source if available.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_place_metadata", "description": "Return game name, place id, and version.", "input_schema": {"type": "object", "properties": {}}},
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
    kv.set(f"credits:{user_id}", {"balance": MAX_CREDITS, "last_updated": 0})

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
    balance, last_updated = get_credits(user["id"])
    next_at = last_updated + CREDIT_INTERVAL_MS if last_updated > 0 else 0

    active_session = kv.get(f"user_plugin:{user['id']}")
    session_id = None
    now = time.time()
    if active_session:
        pid = active_session.get("plugin_id")
        with plugin_registry_lock:
            if pid and pid in plugin_registry and now - plugin_registry[pid]["last_seen"] < 15:
                session_id = active_session.get("session_id")

    return jsonify({
        "user": {"id": user["id"], "email": user["email"]},
        "credits": {"balance": balance, "max": MAX_CREDITS, "next_credit_at": next_at},
        "session_id": session_id,
    })

@app.route("/auth/logout", methods=["POST"])
@require_auth
def auth_logout():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    kv.delete(f"session:{token}")
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

# ═══ AI ROUTES ═══
@app.route("/ai", methods=["POST"])
@require_auth
def ai():
    user = request.user
    ok, balance, _ = use_credit(user["id"])
    if not ok:
        _, _, next_at = get_credits(user["id"])
        return jsonify({
            "error": "no_credits", "balance": 0,
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

    mi = resolve_model(model_key)
    mid, prov = mi["id"], mi["provider"]

    session["accumulated_reply"] = ""

    try:
        if mode == "chat":
            msgs = build_chat_messages(session, user_message, context)
            if prov == "anthropic":
                r = call_anthropic(mid, msgs, tools=TOOL_DEFINITIONS)
                tc = None
                reply_text = ""
                for b in r.content:
                    if b.type == "tool_use":
                        tc = {"id": b.id, "name": b.name, "arguments": b.input}
                    elif b.type == "text":
                        reply_text += b.text
                if tc:
                    session["pending_tool_call"] = tc
                    session["agent_messages"] = msgs + [{"role": "assistant", "content": r.content}]
                    session["status"] = "running"
                    session["latest_reply"] = ""
                    return jsonify({"session_id": session_id, "reply": reply_text or "", "tool_calls": [tc], "plan": None, "status": "tool_requested", "model": mi["label"], "credits": balance - 1})
                session["latest_reply"] = reply_text
                session["status"] = "done"
                return jsonify({"session_id": session_id, "reply": reply_text, "tool_calls": [], "plan": None, "status": "done", "model": mi["label"], "credits": balance - 1})
            else:
                reply = call_gemini(mid, msgs)
                session["latest_reply"] = reply
                session["status"] = "done"
                return jsonify({"session_id": session_id, "reply": reply, "tool_calls": [], "plan": None, "status": "done", "model": mi["label"], "credits": balance - 1})

        elif mode == "agent":
            if prov == "google":
                return jsonify({"session_id": session_id, "reply": "Agent mode requires Claude models.", "tool_calls": [], "plan": None, "status": "done", "model": mi["label"], "credits": balance - 1})
            session["approved"] = False
            session["step_count"] = 0
            session["pending_tool_call"] = None
            session["status"] = "planning"
            pm = build_chat_messages(session, user_message, context)
            pm.append({"role": "user", "content": "Produce a numbered execution plan only. Do not call any tools yet."})
            r = call_anthropic(mid, pm, max_tokens=1200)
            plan = "".join(b.text for b in r.content if b.type == "text")
            session["plan"] = plan
            session["agent_messages"] = pm + [{"role": "assistant", "content": r.content}]
            return jsonify({"session_id": session_id, "reply": "Plan generated.", "tool_calls": [], "plan": plan, "status": "awaiting_approval", "model": mi["label"], "credits": balance - 1})
    except Exception as e:
        return jsonify({"session_id": session_id, "reply": f"Failed: {e}", "tool_calls": [], "plan": None, "status": "error", "credits": balance - 1}), 500

@app.route("/ai/result/<session_id>", methods=["GET"])
@require_auth
def ai_result(session_id):
    session = get_session(session_id)
    tc = session.get("pending_tool_call")
    return jsonify({
        "session_id": session_id,
        "status": session.get("status", "idle"),
        "reply": session.get("latest_reply", ""),
        "pending_tool_call": tc,
        "tool_calls": [tc] if tc else [],
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
        return jsonify({"session_id": session_id, "reply": "Agent requires Claude.", "tool_calls": [], "plan": session.get("plan"), "status": "error"}), 400
    try:
        prior = session.get("agent_messages", [])
        prior.append({"role": "user", "content": "The plan is approved. Start executing now. Use one tool at a time."})
        r = call_anthropic(mi["id"], prior, tools=TOOL_DEFINITIONS)
        session["agent_messages"] = prior + [{"role": "assistant", "content": r.content}]
        tc = None
        ft = ""
        for b in r.content:
            if b.type == "tool_use":
                tc = {"id": b.id, "name": b.name, "arguments": b.input}
            elif b.type == "text":
                ft += b.text
        if tc:
            session["pending_tool_call"] = tc
            return jsonify({"session_id": session_id, "reply": ft or "Executing.", "tool_calls": [tc], "plan": session["plan"], "status": "tool_requested"})
        session["latest_reply"] = ft
        session["status"] = "done"
        return jsonify({"session_id": session_id, "reply": ft, "tool_calls": [], "plan": session["plan"], "status": "done"})
    except Exception as e:
        return jsonify({"session_id": session_id, "reply": f"Failed: {e}", "tool_calls": [], "plan": session.get("plan"), "status": "error"}), 500

# ═══ PLUGIN ROUTES ═══
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
            plugin_registry[pid]["selected_instance"] = data.get("selected_instance")
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
        return jsonify({"reply": "No pending tool call.", "status": "error"}), 400
    session["step_count"] += 1
    session["pending_tool_call"] = None
    try:
        prior = session.get("agent_messages", [])
        ac = [{"type": "tool_use", "id": pc["id"], "name": pc["name"], "input": pc["arguments"]}]
        tr = {"type": "tool_result", "tool_use_id": pc["id"], "content": json.dumps(data.get("tool_result"))}
        cont = prior + [{"role": "assistant", "content": ac}, {"role": "user", "content": [tr]}]
        r = call_anthropic(mi["id"], cont, tools=TOOL_DEFINITIONS)
        session["agent_messages"] = cont + [{"role": "assistant", "content": r.content}]
        nt = None
        ft = ""
        for b in r.content:
            if b.type == "tool_use":
                nt = {"id": b.id, "name": b.name, "arguments": b.input}
            elif b.type == "text":
                ft += b.text
        if ft:
            session["accumulated_reply"] = session.get("accumulated_reply", "") + ft
        if nt:
            session["pending_tool_call"] = nt
            session["status"] = "running"
            return jsonify({"reply": ft or "Tool processed.", "status": "tool_requested", "tool_call": nt})
        session["status"] = "done"
        final_reply = session.get("accumulated_reply", "") or ft
        session["latest_reply"] = final_reply
        session["accumulated_reply"] = ""
        return jsonify({"reply": final_reply, "status": "done"})
    except Exception as e:
        session["status"] = "error"
        return jsonify({"reply": f"Failed: {e}", "status": "error"}), 500

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
