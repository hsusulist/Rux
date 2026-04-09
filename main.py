import os
import json
import uuid
import time
from flask import Flask, request, jsonify, render_template
from anthropic import Anthropic
from threading import Lock

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

if GEMINI_AVAILABLE and GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

MAX_AGENT_STEPS = 20

MODELS = {
    "gemini-flash": {
        "id":       "gemini-3-flash",
        "provider": "google",
        "label":    "Gemini Flash",
        "badge":    "Fast",
    },
    "gemini-pro": {
        "id":       "gemini-3-pro",
        "provider": "google",
        "label":    "Gemini Pro",
        "badge":    "Smart",
    },
    "sonnet": {
        "id":       "claude-sonnet-4.5",
        "provider": "anthropic",
        "label":    "Claude Sonnet",
        "badge":    "Balanced",
    },
    "opus": {
        "id":       "claude-4.5",
        "provider": "anthropic",
        "label":    "Claude Opus",
        "badge":    "Powerful",
    },
}

DEFAULT_MODEL = "sonnet"

sessions = {}
sessions_lock = Lock()
plugin_registry = {}
plugin_registry_lock = Lock()


def log_json(label, data):
    print(f"\n=== {label} ===")
    try:
        print(json.dumps(data, indent=2))
    except Exception:
        print(str(data))


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


def get_session(session_id: str):
    with sessions_lock:
        if session_id not in sessions:
            sessions[session_id] = {
                "conversation": [],
                "agent_messages": [],
                "pending_tool_call": None,
                "plan": None,
                "approved": False,
                "step_count": 0,
                "status": "idle",
                "plugin_id": None,
                "logs": [],
                "latest_reply": "",
                "latest_context": {},
                "model_key": DEFAULT_MODEL,
            }
        return sessions[session_id]


def append_log(session, message: str):
    session["logs"].append(message)
    print(message)


def build_context_from_request(data):
    return {
        "current_script_name": data.get("current_script_name"),
        "current_script_source": data.get("current_script_source"),
        "selected_instance": data.get("selected_instance"),
    }


def build_chat_messages(session, user_message, context):
    content = f"""User message:\n{user_message}\n\nCurrent script name:\n{context.get('current_script_name')}\n\nSelected instance:\n{json.dumps(context.get('selected_instance'), indent=2)}"""
    messages = list(session["conversation"])
    messages.append({"role": "user", "content": content})
    return messages


def call_anthropic_chat(model_id, messages, max_tokens=1500, tools=None):
    if not anthropic_client:
        raise Exception("ANTHROPIC_API_KEY is not set. Add it to your Secrets to use Claude models.")
    kwargs = dict(model=model_id, max_tokens=max_tokens, system=SYSTEM_PROMPT, messages=messages)
    if tools:
        kwargs["tools"] = tools
    return anthropic_client.messages.create(**kwargs)


def call_gemini_chat(model_id, messages, tools=None):
    if not GEMINI_AVAILABLE or not GOOGLE_API_KEY:
        raise Exception("Google API key not configured. Add GOOGLE_API_KEY to your secrets.")

    g_model = genai.GenerativeModel(
        model_name=model_id,
        system_instruction=SYSTEM_PROMPT,
    )

    history = []
    for m in messages[:-1]:
        role = "user" if m["role"] == "user" else "model"
        history.append({"role": role, "parts": [str(m["content"])]})

    chat = g_model.start_chat(history=history)
    last_msg = messages[-1]["content"] if messages else ""
    response = chat.send_message(str(last_msg))
    return response.text


def resolve_model(model_key):
    return MODELS.get(model_key, MODELS[DEFAULT_MODEL])


@app.route("/models", methods=["GET"])
def get_models():
    return jsonify(MODELS)


@app.route("/ai", methods=["POST"])
def ai():
    data = request.get_json(force=True)
    log_json("REQUEST /ai", data)

    session_id   = data.get("session_id") or str(uuid.uuid4())
    mode         = data.get("mode", "chat")
    user_message = data.get("message", "")
    model_key    = data.get("model", DEFAULT_MODEL)
    conversation_history = data.get("conversation_history", [])
    context = build_context_from_request(data)

    session = get_session(session_id)
    session["conversation"] = conversation_history
    session["latest_context"] = context
    session["model_key"] = model_key

    model_info = resolve_model(model_key)
    model_id   = model_info["id"]
    provider   = model_info["provider"]

    try:
        if mode == "chat":
            messages = build_chat_messages(session, user_message, context)

            if provider == "anthropic":
                response = call_anthropic_chat(model_id, messages)
                reply_text = "".join(b.text for b in response.content if b.type == "text")
            else:
                reply_text = call_gemini_chat(model_id, messages)

            session["latest_reply"] = reply_text
            append_log(session, f"[CHAT/{model_info['label']}] {reply_text[:80]}")

            return jsonify({
                "session_id": session_id,
                "reply": reply_text,
                "tool_calls": [],
                "plan": None,
                "status": "done",
                "model": model_info["label"],
            })

        elif mode == "agent":
            if provider == "google":
                return jsonify({
                    "session_id": session_id,
                    "reply": "Agent mode with tool execution is only supported for Claude models (Sonnet / Opus). Please switch to a Claude model.",
                    "tool_calls": [],
                    "plan": None,
                    "status": "done",
                    "model": model_info["label"],
                })

            session["approved"] = False
            session["step_count"] = 0
            session["pending_tool_call"] = None
            session["status"] = "planning"

            planning_messages = build_chat_messages(session, user_message, context)
            planning_messages.append({"role": "user", "content": "Produce a numbered execution plan only. Do not call any tools yet."})

            response = call_anthropic_chat(model_id, planning_messages, max_tokens=1200)
            plan_text = "".join(b.text for b in response.content if b.type == "text")

            session["plan"] = plan_text
            session["agent_messages"] = planning_messages + [{"role": "assistant", "content": response.content}]
            session["latest_reply"] = plan_text

            return jsonify({
                "session_id": session_id,
                "reply": "Plan generated. Awaiting approval.",
                "tool_calls": [],
                "plan": plan_text,
                "status": "awaiting_approval",
                "model": model_info["label"],
            })

        return jsonify({"error": "Invalid mode"}), 400

    except Exception as e:
        error_message = f"Request failed: {str(e)}"
        append_log(session, error_message)
        return jsonify({
            "session_id": session_id,
            "reply": error_message,
            "tool_calls": [],
            "plan": None,
            "status": "error",
        }), 500


@app.route("/ai/approve", methods=["POST"])
def approve_agent():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    session = get_session(session_id)

    # Allow model override on approve
    approve_model = data.get("model")
    if approve_model:
        session["model_key"] = approve_model

    session["approved"] = True
    session["status"] = "running"

    model_key  = session.get("model_key", DEFAULT_MODEL)
    model_info = resolve_model(model_key)
    model_id   = model_info["id"]

    if model_info["provider"] == "google":
        session["status"] = "error"
        return jsonify({
            "session_id": session_id,
            "reply": "Agent mode is only supported for Claude models. Switch to Sonnet or Opus.",
            "tool_calls": [],
            "plan": session.get("plan"),
            "status": "error",
        }), 400

    try:
        # FIX: Use the stored agent_messages from planning phase so Claude
        # retains full context of the plan it wrote
        prior_messages = session.get("agent_messages", [])
        prior_messages.append({"role": "user", "content": "The plan is approved. Start executing now. Use one tool at a time."})

        response = call_anthropic_chat(model_id, prior_messages, tools=TOOL_DEFINITIONS)
        session["agent_messages"] = prior_messages + [{"role": "assistant", "content": response.content}]

        tool_call = None
        final_text = ""
        for block in response.content:
            if block.type == "tool_use":
                tool_call = {"id": block.id, "name": block.name, "arguments": block.input}
            elif block.type == "text":
                final_text += block.text

        if tool_call:
            session["pending_tool_call"] = tool_call
            append_log(session, f"[AGENT TOOL] {tool_call['name']}")
            return jsonify({
                "session_id": session_id,
                "reply": final_text or "Executing first step.",
                "tool_calls": [tool_call],
                "plan": session["plan"],
                "status": "tool_requested",
            })

        session["latest_reply"] = final_text
        session["status"] = "done"
        return jsonify({
            "session_id": session_id,
            "reply": final_text,
            "tool_calls": [],
            "plan": session["plan"],
            "status": "done",
        })

    except Exception as e:
        error_message = f"Approval/start failed: {str(e)}"
        append_log(session, error_message)
        return jsonify({
            "session_id": session_id,
            "reply": error_message,
            "tool_calls": [],
            "plan": session.get("plan"),
            "status": "error",
        }), 500


@app.route("/plugin/heartbeat", methods=["POST"])
def plugin_heartbeat():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    plugin_id  = data.get("plugin_id")
    session = get_session(session_id)
    session["plugin_id"]     = plugin_id
    session["plugin_status"] = data.get("status")
    session["selected_instance"] = data.get("selected_instance")

    with plugin_registry_lock:
        plugin_registry[plugin_id] = {
            "session_id": session_id,
            "plugin_id":  plugin_id,
            "last_seen":  time.time(),
            "status":     data.get("status"),
            "selected_instance": data.get("selected_instance"),
        }

    return jsonify({"ok": True})


@app.route("/plugin/poll", methods=["POST"])
def plugin_poll():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    session = get_session(session_id)
    return jsonify({
        "status_message": session["status"],
        "tool_call":      session.get("pending_tool_call"),
    })


@app.route("/plugin/tool_result", methods=["POST"])
def plugin_tool_result():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    session = get_session(session_id)

    # Guard: only Claude models support tool-use loop
    model_key = session.get("model_key", DEFAULT_MODEL)
    model_info = resolve_model(model_key)
    if model_info["provider"] == "google":
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({"reply": "Tool execution is not supported for Google models.", "status": "error"}), 400

    if session["step_count"] >= MAX_AGENT_STEPS:
        session["status"] = "error"
        session["pending_tool_call"] = None
        return jsonify({"reply": "Max agent step limit reached.", "status": "error"}), 400

    tool_name    = data.get("tool_name")
    tool_result  = data.get("tool_result")
    pending_call = session.get("pending_tool_call")

    if not pending_call:
        return jsonify({"reply": "No pending tool call.", "status": "error"}), 400

    session["step_count"] += 1
    session["pending_tool_call"] = None

    model_id = model_info["id"]

    try:
        prior_messages = session.get("agent_messages", [])
        assistant_content = [{"type": "tool_use", "id": pending_call["id"], "name": pending_call["name"], "input": pending_call["arguments"]}]
        tool_result_content = {"type": "tool_result", "tool_use_id": pending_call["id"], "content": json.dumps(tool_result)}
        continued_messages = prior_messages + [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": [tool_result_content]},
        ]

        response = call_anthropic_chat(model_id, continued_messages, tools=TOOL_DEFINITIONS)
        session["agent_messages"] = continued_messages + [{"role": "assistant", "content": response.content}]

        next_tool  = None
        final_text = ""
        for block in response.content:
            if block.type == "tool_use":
                next_tool = {"id": block.id, "name": block.name, "arguments": block.input}
            elif block.type == "text":
                final_text += block.text

        if next_tool:
            session["pending_tool_call"] = next_tool
            session["status"] = "running"
            return jsonify({"reply": final_text or f"Tool {tool_name} processed.", "status": "tool_requested", "tool_call": next_tool})

        session["status"] = "done"
        session["latest_reply"] = final_text
        return jsonify({"reply": final_text, "status": "done"})

    except Exception as e:
        session["status"] = "error"
        return jsonify({"reply": f"Tool loop failed: {str(e)}", "status": "error"}), 500


@app.route("/status", methods=["GET"])
def get_status():
    now = time.time()
    with plugin_registry_lock:
        active = [p for p in plugin_registry.values() if now - p["last_seen"] < 10]
    latest = active[0] if active else None
    return jsonify({
        "plugin_connected": bool(active),
        "plugin_count":     len(active),
        "selected_instance": latest["selected_instance"] if latest else None,
        "plugin_status":     latest["status"] if latest else None,
        "status": "idle",
    })


@app.route("/session/<session_id>", methods=["GET"])
def get_session_state(session_id):
    session = get_session(session_id)
    return jsonify({
        "status":            session.get("status"),
        "plan":              session.get("plan"),
        "latest_reply":      session.get("latest_reply"),
        "pending_tool_call": session.get("pending_tool_call"),
        "logs":              session.get("logs", []),
        "step_count":        session.get("step_count", 0),
    })


@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/app")
def chat_app():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)